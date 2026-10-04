# -*- coding: utf-8 -*-
"""S4 追更重塑回归（白皮书 §5.4 能力 + A6）：

① facts.watched_progress：逐用户 IsPlayed 最高已播集、单轮结果缓存、
   无记录返回 None（不冒充）、Emby 失败抛 EmbyFactError（不冒充「没看过」）；
② D4 并用、手动优先：effective_watched；手动值坏格式回退自动值；
③ 通知规则：新增集 > 生效已看才算「值得提醒」，全部新增仍记录（new_eps 不丢）；
④ 入库签名触发：_tv_sig 明细签名（「总数反减」场景——入库 3 集又删 1 集总数
   不变但签名变）、仅受影响订阅触发、受影响为空跳过；
⑤ 新文案：《剧》你已看到 E15，新入库 E16–E17（共 N 集，已播 M）；
   手动已看优先于自动的文案用例。

Emby / TMDB / Telegram 假实现替换，不联网。
"""
import os

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import config, emby, facts, ingest, state, storage, subscribe, tg, tmdb
from app.tgmsg import blocks, reports

TMDB_ID = '8888'
SID = 's4_sub1'
SUB_STORE = 's4_sub_state'


def _ep(season, episode, **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode,
         'DateCreated': '2026-01-01T00:00:00Z'}
    d.update(extra)
    return d


def _full(seasons):
    return [_ep(s, e) for s, n in seasons.items() for e in range(1, n + 1)]


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """独立 STATE_DIR（订阅状态/事实库）+ /media 路径映射。"""
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    monkeypatch.setattr(state, 'L_ROOT', tmp_path / 'local')
    monkeypatch.setattr(state, 'S_ROOT', tmp_path / 'share')
    monkeypatch.setattr(state, 'EMBY_PATHS', state.emby_path_map('/media/local', '/media/share'))
    yield sd
    storage.close_all()


def _tv_info(status='Ended', total=25):
    aired = {(1, e) for e in range(1, total + 1)}
    return {'name': '测试剧', 'status': status, 'aired': aired,
            'total_episodes': total, 'declared_total': total,
            'seasons': {1: {'episode_count': total, 'air_date': ''}}}


class _Env:
    """假 Emby（Series/Episode/Users/播放记录）+ 假 Telegram/TMDB + 独立订阅存储。"""

    def __init__(self):
        self.episodes = []
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/测试剧',
                              'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.users = [{'Id': 'u1', 'Name': '甲'}]
        self.watched = {}                   # uid -> [Episode items]
        self.sent = []
        self.notify_ok = True
        self.calls = {'watch': 0}           # IsPlayed 查询计数（缓存验证用）

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if path == '/Users':
            return list(self.users)
        if path.startswith('/Users/') and path.endswith('/Items'):
            uid = path.split('/')[2]
            if params.get('Filters') == 'IsPlayed':
                self.calls['watch'] += 1
                return {'Items': list(self.watched.get(uid) or [])}
            return {'Items': []}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            return {'Items': list(self.episodes)}
        return {}

    def _notify(self, text, chat_id=None):
        self.sent.append(text)
        return self.notify_ok

    def __enter__(self):
        self._orig = (emby.emby_request, tg.notify_telegram, tmdb.Tmdb,
                      state.SUB_STORE, subscribe.get_subscriptions())
        emby.emby_request = self._emby
        tg.notify_telegram = self._notify

        class FakeTmdb:
            def get(self, path, ttl=0, **kw):
                return None

            def save(self):
                pass
        tmdb.Tmdb = FakeTmdb
        state.SUB_STORE = SUB_STORE
        subscribe.reset_sub_state()
        subscribe.set_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧',
                                      'enabled': True}])
        facts.invalidate_watch_cache()
        return self

    def __exit__(self, *exc):
        (emby.emby_request, tg.notify_telegram, tmdb.Tmdb,
         state.SUB_STORE, subs) = self._orig
        subscribe.reset_sub_state()
        subscribe.set_subscriptions(subs)
        facts.invalidate_watch_cache()

    def state(self):
        return subscribe.load_sub_state().get(SID) or {}


# ═══════════ ① watched_progress ═══════════

class TestWatchedProgress:

    def test_highest_watched_across_users(self, isolated_db):
        with _Env() as env:
            env.watched = {'u1': [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 15, 'DatePlayed': '2026-01-02'},
                                  {'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 8, 'DatePlayed': '2026-01-01'}],
                           'u2': [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 20, 'DatePlayed': '2026-01-03'}]}
            env.users.append({'Id': 'u2', 'Name': '乙'})
            best = facts.watched_progress(TMDB_ID, use_round_cache=False)
            assert best['episode'] == (1, 20)          # 跨用户取最高
            assert best['date_played'] == '2026-01-03'

    def test_no_records_returns_none(self, isolated_db):
        with _Env() as env:
            env.watched = {'u1': []}
            assert facts.watched_progress(TMDB_ID, use_round_cache=False) is None

    def test_round_cache_reuses_result(self, isolated_db):
        """单轮缓存：同剧第二次查询不打 Emby（规避撤回版每订阅全量查 /Users）。"""
        with _Env() as env:
            env.watched = {'u1': [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 10}]}
            facts.invalidate_watch_cache()
            r1 = facts.watched_progress(TMDB_ID)
            r2 = facts.watched_progress(TMDB_ID)
            assert r1['episode'] == (1, 10) and r2['episode'] == (1, 10)
            assert env.calls['watch'] == 1             # 只打了一次 IsPlayed
            env.watched['u1'] = [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                  'IndexNumber': 16}]
            assert facts.watched_progress(TMDB_ID)['episode'] == (1, 10)  # 仍读缓存
            facts.invalidate_watch_cache()
            assert facts.watched_progress(TMDB_ID)['episode'] == (1, 16)  # 失效后现查

    def test_emby_failure_raises_not_none(self, isolated_db):
        with _EmbyBoomEnv():
            with pytest.raises(facts.EmbyFactError):
                facts.watched_progress(TMDB_ID, use_round_cache=False)


class _EmbyBoomEnv:
    def __enter__(self):
        self._orig = emby.emby_request

        def boom(path, params=None, method='GET', timeout=15):
            raise RuntimeError('Emby 无法连接')
        emby.emby_request = boom
        return self

    def __exit__(self, *exc):
        emby.emby_request = self._orig
        return False


# ═══════════ ② D4 手动优先 ═══════════

class TestManualPriority:

    def test_effective_watched_manual_overrides_auto(self):
        assert facts.effective_watched('S01E15', (1, 20)) == (1, 15)
        assert facts.effective_watched((1, 12), (1, 20)) == (1, 12)

    def test_effective_watched_falls_back_to_auto(self):
        assert facts.effective_watched('', (1, 9)) == (1, 9)
        assert facts.effective_watched(None, (1, 9)) == (1, 9)
        assert facts.effective_watched(None, None) is None
        assert facts.effective_watched('不是集号', (1, 7)) == (1, 7)   # 坏格式回退
        assert facts.effective_watched((0, 5), (1, 7)) == (1, 7)      # 非法季回退


# ═══════════ ③ 通知规则与文案 ═══════════

class TestNotifyRuleAndCopy:

    def _baseline(self, env, watched=None):
        env.watched = {'u1': watched or []}
        env.episodes = _full({1: 15})
        assert subscribe.check_subscriptions(send_notify=True)['updates'] == []

    def test_new_eps_above_watch_notified_and_recorded(self, isolated_db):
        with _Env() as env:
            self._baseline(env, watched=[{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                          'IndexNumber': 15}])
            env.episodes = _full({1: 18})                 # 入库 E16–E18，已看 E15
            r = subscribe.check_subscriptions(send_notify=True)
            u = r['updates'][0]
            assert u['new_eps'] == ['S01E16', 'S01E17', 'S01E18']   # 全部记录不丢
            assert u['watched_ep'] == 'S01E15'
            assert u['watched_source'] == 'auto'
            assert u['worth_notifying'] is True
            assert 'S01E16–E18' in env.sent[-1]                     # 区间文案

    def test_manual_watched_used_in_copy(self, isolated_db):
        """手动已看优先于自动：文案用手动值（A6：你已看到 E15）。"""
        with _Env() as env:
            env.watched = {'u1': [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 12}]}          # 自动值 E12
            env.episodes = _full({1: 15})
            assert subscribe.check_subscriptions(send_notify=True)['updates'] == []
            env.episodes = _full({1: 18})
            subscribe.set_subscriptions([{'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧',
                                          'enabled': True,
                                          'watched_ep_manual': 'S01E15'}])
            r = subscribe.check_subscriptions(send_notify=True)
            u = r['updates'][0]
            assert u['watched_ep'] == 'S01E15'
            assert u['watched_source'] == 'manual'
            # 文案：手动值 E15 优先于自动值 E12
            text = env.sent[-1]
            assert '你已看到 <b>S01E15</b>' in text
            assert '新入库 <b>S01E16–E18</b>' in text
            assert 'S01E12' not in text

    def test_new_eps_below_watch_still_recorded(self, isolated_db):
        """新增集全部 ≤ 已看进度：不值得提醒（不提示新入库诱导），但仍记账不丢。"""
        with _Env() as env:
            env.watched = {'u1': [{'SeriesId': 'S1', 'ParentIndexNumber': 1,
                                   'IndexNumber': 30}]}
            env.episodes = _full({1: 15})
            assert subscribe.check_subscriptions(send_notify=True)['updates'] == []
            env.episodes = _full({1: 18})
            r = subscribe.check_subscriptions(send_notify=True)
            u = r['updates'][0]
            assert u['new_eps'] == ['S01E16', 'S01E17', 'S01E18']    # 记账不丢
            assert u['worth_notifying'] is False
            assert '都不超过已看进度' in env.sent[-1]

    def test_copy_contains_totals(self, isolated_db):
        """文案尾部「（共 N 集，已播 M）」。"""
        with _Env() as env:
            env.episodes = _full({1: 15})
            assert subscribe.check_subscriptions(send_notify=True)['updates'] == []
            # TMDB 打桩提供 declared/aired
            class FakeTmdb:
                def get(self, path, ttl=0, **kw):
                    return {'name': '测试剧', 'status': 'Ended',
                            'seasons': [{'season_number': 1, 'episode_count': 28}],
                            'last_episode_to_air': {'season_number': 1, 'episode_number': 17}}

                def save(self):
                    pass
            old = tmdb.Tmdb
            tmdb.Tmdb = FakeTmdb
            try:
                env.episodes = _full({1: 18})
                r = subscribe.check_subscriptions(send_notify=True)
                u = r['updates'][0]
                assert u['total_eps'] == 28 and u['aired_total'] == 17
                text = env.sent[-1]
                assert '共 28 集' in text and '已播 17' in text
            finally:
                tmdb.Tmdb = old


# ═══════════ ④ 入库签名触发 ═══════════

def _ingest_data(rows, ts=1000.0):
    """构造 _fetch_ingest 形状的数据：rows = [(src, cat, title, count, last_ts)]"""
    tv_detail = {}
    for src, cat, title, count, last_ts in rows:
        shows = tv_detail.setdefault(src, {}).setdefault(cat, {})
        shows[title] = {'count': count, 'last_ts': last_ts, 'seasons': {}}
    return {'ts': ts, 'ok': True, 'stats': {}, 'tree': {'tv_detail': tv_detail}}


class TestIngestSignature:

    def test_sig_detects_count_change_at_same_total(self, isolated_db):
        """「总数反减」场景：入库 3 集又删 1 集 → 总数 3→2 反而变小；总数不变
        （入库 2 删 2）但明细变 → 签名必须变（旧「只比总数」口径的教训）。"""
        old = _ingest_data([('local', '国产剧', '测试剧', 2, 100.0)])
        same_total = _ingest_data([('local', '国产剧', '测试剧', 2, 200.0)])   # 总数同、时间变
        assert ingest._tv_sig(same_total) != ingest._tv_sig(old)
        grown = _ingest_data([('local', '国产剧', '测试剧', 5, 300.0)])
        assert ingest._tv_sig(grown) != ingest._tv_sig(old)

    def test_sig_unchanged_skips(self, isolated_db):
        old = _ingest_data([('local', '国产剧', '测试剧', 2, 100.0)])
        new = _ingest_data([('local', '国产剧', '测试剧', 2, 100.0)])
        assert ingest._tv_sig_affected_subscriptions(new, old) is None

    def test_affected_subscriptions_matched_by_name(self, isolated_db):
        """签名变化 + 入库剧名命中订阅 → 触发；不命中 → 空集跳过。"""
        old = _ingest_data([])
        new = _ingest_data([('local', '国产剧', '测试剧', 3, 200.0)])
        with _Env():
            r = ingest._tv_sig_affected_subscriptions(new, old)
            assert r == {'测试剧'}                     # 订阅名「测试剧」命中
        new2 = _ingest_data([('local', '国产剧', '别的剧', 3, 200.0)])
        with _Env():
            r2 = ingest._tv_sig_affected_subscriptions(new2, old)
            assert r2 == set()                         # 无受影响订阅 → 跳过

    def test_refresh_ingest_triggers_check_for_affected(self, isolated_db, monkeypatch):
        """端到端：refresh_ingest_cache 签名变化 → 调 check_subscriptions 一次；
        无受影响订阅 → 不调用。"""
        calls = {'n': 0}
        monkeypatch.setattr(subscribe, 'check_subscriptions',
                            lambda send_notify=True: calls.__setitem__('n', calls['n'] + 1) or
                            {'updates': [], 'total': 0, 'checked': 0, 'errors': []})
        old = _ingest_data([])
        new = _ingest_data([('local', '国产剧', '测试剧', 3, 200.0)])
        monkeypatch.setattr(ingest, '_fetch_ingest', lambda hours=24: new)
        monkeypatch.setattr(ingest, 'read_ingest_cache', lambda max_age=None: old)
        with _Env():
            ingest.refresh_ingest_cache()
            assert calls['n'] == 1                     # 订阅命中 → 触发

        monkeypatch.setattr(ingest, '_fetch_ingest', lambda hours=24: new)
        monkeypatch.setattr(ingest, 'read_ingest_cache', lambda max_age=None: old)
        monkeypatch.setattr(subscribe, 'get_subscriptions',
                            lambda: [{'id': 'x', 'tmdb_id': '9', 'name': '无关剧', 'enabled': True}])
        ingest.refresh_ingest_cache()
        assert calls['n'] == 1                         # 无命中 → 未再触发


# ═══════════ ⑤ 订阅卡字段透出 ═══════════

class TestSubCardWatched:

    def test_save_preserves_manual_field(self, isolated_db):
        from app.routers.subscribe import api_save_subs
        out = api_save_subs({'subscriptions': [
            {'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True,
             'watched_ep_manual': 'S01E15'}]})
        assert out['status'] == 'success'
        saved = {s['id']: s for s in subscribe.get_subscriptions()}
        assert saved[SID]['watched_ep_manual'] == 'S01E15'

    def test_get_subs_exposes_manual_field(self, isolated_db):
        from app.routers.subscribe import api_save_subs, api_get_subs
        api_save_subs({'subscriptions': [
            {'id': SID, 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True,
             'watched_ep_manual': 'S01E15'}]})
        r = api_get_subs()
        assert r['subscriptions'][0]['watched_ep_manual'] == 'S01E15'
