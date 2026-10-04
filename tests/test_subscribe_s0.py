# -*- coding: utf-8 -*-
"""S0 止血回归：发送/记账正交开关、新订阅播种、失败显式化、治理结果弹层。

对应白皮书 §5.4 修复1/3/4 与 §5.7（P1-A / P1-B / P1-C / P2）：
  ① E01–E25 播种后再入 E26–E28，期间保存/暂停任意订阅不丢失通知（A1）
  ② send_notify=False 不写 notified_episodes（红线：只读预览绝不落盘）
  ③ 断开 Emby / 清空 Key 时「立即检查」返回 {ok:false, reason}（A2）
  ④ 治理结果弹层渲染契约（P2，正则级，同 test_frontend_xss 手法）

Emby / Telegram / TMDB 全部用假实现替换，不联网。
"""
import os
import re
from pathlib import Path

# app/routers/deps 在导入时读 WEB_PASSWORD：不设置会 SystemExit，测试进程统一给个默认值
os.environ.setdefault('WEB_PASSWORD', 's0-test')

from app import config, emby, state, subscribe, tg, tmdb

TMDB_ID = '8888'
SID = 's0_sub1'
SUB_STORE = 's0_subscriptions_state'


def _ep(season, episode, **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode, 'DateCreated': '2026-01-01T00:00:00Z'}
    d.update(extra)
    return d


def _full(seasons: dict):
    return [_ep(s, e) for s, n in seasons.items() for e in range(1, n + 1)]


class _Env:
    """替换 emby_request / notify_telegram / Tmdb / 订阅与状态存储，结束时全部还原。"""

    def __init__(self):
        self.episodes = []
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.sent = []
        self.notify_ok = True
        self._orig = None

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            return {'Items': list(self.episodes)}
        return {}

    def _notify(self, text, chat_id=None):
        self.sent.append(text)
        return self.notify_ok

    def __enter__(self):
        env = self

        class FakeTmdb:
            def get(self, path, ttl=0, **kw):
                return None

            def save(self):
                pass

        self._orig = (emby.emby_request, tmdb.Tmdb, tg.notify_telegram,
                      state.SUB_STORE, subscribe.get_subscriptions())
        self._orig_key = state.EMBY_KEY
        self._orig_cfg = {k: config.load_config().get(k)
                          for k in ('subscribe_enabled', 'subscribe_check_tmdb')}
        emby.emby_request = self._emby
        tmdb.Tmdb = FakeTmdb
        tg.notify_telegram = self._notify
        state.SUB_STORE = SUB_STORE
        subscribe.reset_sub_state()

        def _on(cfg):
            cfg['subscribe_enabled'] = '1'
            cfg['subscribe_check_tmdb'] = '0'   # S0 语义只关心集合差集与记账，不引入 TMDB 缺集
        config.update_config(_on)
        subscribe.set_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True}])
        return self

    def __exit__(self, *exc):
        subscribe.reset_sub_state()
        (emby.emby_request, tmdb.Tmdb, tg.notify_telegram,
         state.SUB_STORE, subs) = self._orig
        state.EMBY_KEY = self._orig_key

        def _restore(cfg):
            for k, v in self._orig_cfg.items():
                if v is None: cfg.pop(k, None)
                else: cfg[k] = v
        config.update_config(_restore)
        subscribe.set_subscriptions(subs)
        return False

    def state(self):
        return subscribe.load_sub_state().get(SID) or {}


class TestSeedAndNoLostNotification:
    """① E01–E25 播种 → 再入 E26–E28；期间保存/暂停订阅不丢通知、不重播种（A1）。"""

    def test_seed_baseline_then_new_eps_notified(self):
        with _Env() as env:
            env.episodes = _full({1: 25})
            r = subscribe.seed_new_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧'}])
            assert r['seeded'] == 1 and r['errors'] == []
            st = env.state()
            assert len(st['notified_episodes']) == 25 and st['latest_ep'] == 'S01E25'

            # 补入 E26–E28：即使期间把订阅整份重存 / 暂停再启用，通知也不能丢
            env.episodes = _full({1: 28})
            from app.routers.subscribe import api_save_subs
            subs_payload = [{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧',
                             'poster': '', 'enabled': False, 'added_at': '2026-10-04 00:00:00'}]
            out = api_save_subs({'subscriptions': subs_payload})
            assert out['status'] == 'success' and 'warning' not in out
            assert len(env.state()['notified_episodes']) == 25      # 重存不重播种、不动基线

            # 暂停期间检查：跳过该订阅，什么也不发
            assert subscribe.check_subscriptions(send_notify=True)['updates'] == []
            assert env.sent == []

            # 重新启用（再走一次保存）→ 通知 E26–E28，且不重复播报已播种的 E01–E25
            subs_payload[0]['enabled'] = True
            api_save_subs({'subscriptions': subs_payload})
            res = subscribe.check_subscriptions(send_notify=True)
            assert res['updates'] and res['updates'][0]['new_eps'] == ['S01E26', 'S01E27', 'S01E28']
            assert len(env.sent) == 1
            # A1 文案口径：新增入库 S01E26–E28（区间精确，不逐集罗列）
            assert 'S01E26–E28' in env.sent[-1]
            assert env.state()['latest_ep'] == 'S01E28'

    def test_seed_failure_is_reported_not_swallowed(self):
        with _Env() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('连接被拒绝')
            emby.emby_request = boom
            r = subscribe.seed_new_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧'}])
            assert r['seeded'] == 0
            assert r['errors'] and '连接被拒绝' in r['errors'][0]['message']
            # 播种失败不写该订阅的基线（状态机会按全新订阅安静播种，不刷屏）
            assert env.state() == {}

    def test_seed_skips_existing_baseline(self):
        with _Env() as env:
            env.episodes = _full({1: 25})
            assert subscribe.seed_new_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID}])['seeded'] == 1
            env.episodes = _full({1: 30})
            # 已有基线的订阅重复保存（toggleSubEnabled / removeSub 都会整份重存）不重播种
            assert subscribe.seed_new_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID}])['seeded'] == 0
            assert len(env.state()['notified_episodes']) == 25


class TestSendAndRecordOrthogonal:
    """② 红线：send_notify=False 只读预览绝不落盘；self_delivered 送达确认后才记账。"""

    def _baseline(self, env, total=15):
        env.episodes = _full({1: total})
        assert subscribe.check_subscriptions(send_notify=True)['updates'] == []
        return subscribe.load_sub_state()

    def test_preview_never_persists(self):
        with _Env() as env:
            before = self._baseline(env)
            before_snap = dict(before.get(SID) or {})

            env.episodes = _full({1: 18})          # 新入 E16–E18
            r = subscribe.check_subscriptions(send_notify=False)
            # 预览返回「本应通知什么」
            assert r['updates'] and r['updates'][0]['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert r.get('sent') is None and 'staged' not in r and not r['errors']
            # 红线：状态一个字节都没动
            after = subscribe.load_sub_state().get(SID) or {}
            assert after == before_snap
            assert len(after['notified_episodes']) == 15 and after['latest_ep'] == 'S01E15'
            assert env.sent == []                   # 也没发 Telegram

    def test_self_delivered_records_only_after_commit(self):
        with _Env() as env:
            self._baseline(env)
            env.episodes = _full({1: 18})
            # Bot /sub check：self_delivered=True 只算不写，staged 随返回值带回
            r = subscribe.check_subscriptions(send_notify=False, self_delivered=True)
            assert r['updates'] and 'staged' in r
            after = subscribe.load_sub_state().get(SID) or {}
            assert after['latest_ep'] == 'S01E15'   # 未送达前不落盘

            # 回显发送成功 → commit → 才记账
            assert subscribe.commit_sub_check(r['staged']) is True
            assert env.state()['latest_ep'] == 'S01E18'
            # 记账后重复检查无变化，不重复播报
            assert subscribe.check_subscriptions(send_notify=True)['updates'] == []

            # commit 幂等且对空 staged 无害
            assert subscribe.commit_sub_check({}) is False

    def test_send_failure_rolls_back(self):
        with _Env() as env:
            self._baseline(env)
            env.episodes = _full({1: 18})
            env.notify_ok = False
            r = subscribe.check_subscriptions(send_notify=True)
            assert r['updates'] and r['sent'] is False
            assert env.state()['latest_ep'] == 'S01E15'   # 回滚，下一轮重试
            env.notify_ok = True
            r = subscribe.check_subscriptions(send_notify=True)
            assert r['sent'] is True and env.state()['latest_ep'] == 'S01E18'


class TestFailureExplicit:
    """③ Emby 断开 / Key 未配置：结构化 errors；「立即检查」返回 {ok:false, reason}（A2）。"""

    def test_check_returns_structured_errors(self):
        with _Env() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('连接超时')
            emby.emby_request = boom
            r = subscribe.check_subscriptions(send_notify=True)
            assert r['updates'] == [] and r['checked'] == 0
            assert len(r['errors']) == 1
            assert r['errors'][0]['sid'] == SID and '连接超时' in r['errors'][0]['message']
            assert 'name' in r['errors'][0] and 'message' in r['errors'][0]

    def test_check_now_endpoint_returns_ok_false_reason(self):
        with _Env() as env:
            from app.routers.subscribe import api_subs_check_now

            def boom(path, params=None, method='GET', timeout=15):
                if not state.EMBY_KEY:                # 与真 emby_request 同款拒绝行为
                    raise RuntimeError('未配置 EMBY_KEY')
                raise RuntimeError('连接被拒绝')
            emby.emby_request = boom

            # 清空 Key：emby_request 直接拒绝
            state.EMBY_KEY = ''
            out = api_subs_check_now()
            assert out['status'] == 'error' and out['ok'] is False
            assert 'EMBY_KEY' in out['reason']

            # 断开 Emby：请求层失败同样显式报错，而不是「无变化」
            state.EMBY_KEY = 'k'
            out = api_subs_check_now()
            assert out['status'] == 'error' and out['ok'] is False
            assert '连接被拒绝' in out['reason']

    def test_partial_failure_still_succeeds_with_errors(self):
        with _Env() as env:
            calls = {'n': 0}

            def flaky(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Series':
                    calls['n'] += 1
                    if calls['n'] >= 2:               # 第二个订阅的精确查询失败
                        raise RuntimeError('连接被拒绝')
                    return {'Items': [{'Id': 'S1', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}}]}
                if params.get('IncludeItemTypes') == 'Episode':
                    return {'Items': _full({1: 25})}
                return {}

            subscribe.set_subscriptions([
                {'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True},
                {'id': 's0_sub2', 'tmdb_id': '7777', 'name': '倒霉剧', 'enabled': True},
            ])
            emby.emby_request = flaky
            try:
                r = subscribe.check_subscriptions(send_notify=True)
                assert r['checked'] == 1 and len(r['errors']) == 1
                assert r['errors'][0]['sid'] == 's0_sub2'
                # 好的那个订阅照常播种 + 记账
                assert len(env.state()['notified_episodes']) == 25
            finally:
                subscribe.set_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True}])


class TestSubCardLiveStatus:
    """P1-B：卡片「最新集」来自实时现状 + 数据时间，记账值降为 notified_ep 小字。"""

    def test_live_ep_and_notified_ep_are_separate(self):
        with _Env() as env:
            env.episodes = _full({1: 25})
            subscribe.seed_new_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID}])
            env.episodes = _full({1: 28})            # Emby 已有 28 集，记账还停在 25

            from app.routers.subscribe import api_get_subs
            r = api_get_subs()
            s = r['subscriptions'][0]
            assert s['live_ep'] == 'S01E28' and s['live_eps'] == 28
            assert s['notified_ep'] == 'S01E25'      # 记账值单独一行小字
            assert s['data_time']

    def test_live_query_failure_marks_data_error(self):
        with _Env() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('Emby 无法连接')
            emby.emby_request = boom
            from app.routers.subscribe import api_get_subs
            r = api_get_subs()
            s = r['subscriptions'][0]
            assert s.get('data_error') and 'Emby 无法连接' in s['data_error']
            assert s.get('live_ep', '') == ''


class TestCleanResultModal:
    """④ P2 治理弹层渲染契约（正则级，同 test_frontend_xss 手法）。"""

    HTML = (Path(__file__).resolve().parent.parent / 'static' / 'index.html').read_text(encoding='utf-8')

    def test_alert_replaced_by_in_app_modal(self):
        assert "alert('【执行结果】" not in self.HTML
        run_clean = re.search(r"async function runClean\(\)\{.*?\n\}", self.HTML, re.S)
        assert run_clean and 'alert(' not in run_clean.group(0)
        assert 'showCleanResultModal(t.result)' in run_clean.group(0)

    def test_modal_contract(self):
        html = self.HTML
        assert 'function showCleanResultModal(' in html
        # 顶部汇总四格 + Emby 刷新
        for chip in ('释放本地', '淘汰分享', '跳过', '失败', 'Emby 刷新'):
            assert chip in html
        # 明细可折叠、可滚动、可复制
        assert re.search(r"showCleanResultModal[\s\S]*?<details open[\s\S]*?max-height:45vh;overflow-y:auto", html)
        assert 'function copyCleanDetail(' in html and '复制明细' in html
        # 失败原因块与「在执行记录中查看」跳转（与 write_audit_log 同一份数据）
        assert '失败原因' in html
        assert "switchTab(\\'history\\')" in html
        # 兜底复制（navigator.clipboard 不可用时）
        assert 'function fallbackCopy(' in html
