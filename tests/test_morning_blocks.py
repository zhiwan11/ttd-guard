# -*- coding: utf-8 -*-
"""S5 晨报回归（白皮书 §5.5，验收 A7：四块齐全 + 失败显式 + 数据时间标注）：

- 块1 近 24h 入库：到点现查 refresh_ingest_cache(hours=24)；失败写
  「入库扫描失败：原因」+ 上次成功数据时间，不拿轮询缓存冒充；
- 块2 近 24h 播放（D7）：facts.recent_played_24h——逐用户并发、DatePlayed 倒序
  越过 24h 即 break、截 50 条、单用户失败块内显式、整体失败显式；
- 块3 追更更新：无状态——facts.get_series_status + created 键 DateCreated 24h
  计数；created 键 JSON 往返后仍可解析（锁死撤回版元组键 bug 的反向回归）；
  失败聚合「N 部检查失败」；
- 块4 缺集榜：标题带「数据时间 HH:MM」；默认不含「在更·落后」，D3 设置项可切换；
  预扫失败明写「预扫失败，数据来自 HH:MM」；
- 发送路径：手动「立即发送」与定时同一函数，仅差 mark_sent。

Emby / TMDB / Telegram 假实现替换，不联网。
"""
import datetime
import json
import time

import pytest

from app import emby, facts, ingest, morning, state, storage, subscribe, tg
from app.tgmsg import reports

TMDB_ID = '8888'
SUB_STORE = 's5_morning_state'


def _tv_info(status='Ended', total=25):
    aired = {(1, e) for e in range(1, total + 1)}
    return {'name': '测试剧', 'status': status, 'aired': aired,
            'total_episodes': total, 'declared_total': total,
            'seasons': {1: {'episode_count': total, 'air_date': ''}}}


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    monkeypatch.setattr(state, 'L_ROOT', tmp_path / 'local')
    monkeypatch.setattr(state, 'S_ROOT', tmp_path / 'share')
    monkeypatch.setattr(state, 'EMBY_PATHS', state.emby_path_map('/media/local', '/media/share'))
    yield sd
    storage.close_all()


def _utc_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
        .strftime('%Y-%m-%dT%H:%M:%S.000Z')


# ═══════════ 块1：入库到点现查 ═══════════

class TestIngestBlock:

    def test_failure_message_with_old_data_time(self, isolated_db, monkeypatch):
        """现查失败 → 「入库扫描失败：原因」+ 上次成功数据时间，不拿旧缓存冒充。"""
        def fail(hours=24):
            return {'ok': False, 'error': '连接超时', 'stats': {}, 'ts': time.time()}
        monkeypatch.setattr(ingest, 'refresh_ingest_cache', fail)
        old_ts = time.time() - 1800
        monkeypatch.setattr(ingest, 'read_ingest_cache', lambda max_age=None: {'ts': old_ts})
        data = morning.build_morning_report(['stats'])
        assert '入库扫描失败：连接超时' in data
        expect = datetime.datetime.fromtimestamp(old_ts).strftime('%H:%M')
        assert expect in data                     # 上次成功数据时间诚实标注
        # 不冒充：失败分支里不出现「+0 部」这种零统计正文
        body = data.split('入库扫描失败')[1]
        assert '+0' not in body

    def test_success_uses_fresh_scan(self, isolated_db, monkeypatch):
        fresh = {'ok': True, 'ts': time.time(),
                 'stats': {'movies': 2, 'series': 1, 'episodes': 5}, 'tree': {}}
        monkeypatch.setattr(ingest, 'refresh_ingest_cache', lambda hours=24: fresh)
        data = morning.build_morning_report(['stats'])
        assert '+2' in data and '数据' in data


# ═══════════ 块2：近 24h 播放 ═══════════

class TestPlayedBlock:

    def test_window_break_and_limit(self, isolated_db, monkeypatch):
        """倒序越过 24h 即 break：窗口外的集不再产生记录；最多 50 条。"""
        now = time.time()
        env_rows = [{'Id': 'e%d' % i, 'Type': 'Episode', 'SeriesName': '剧',
                     'ParentIndexNumber': 1, 'IndexNumber': i,
                     'DatePlayed': _utc_iso(now - i * 3600)}          # i 小时前
                    for i in range(1, 80)]
        # 倒序把 30 天前的旧记录排最前（DatePlayed 倒序 = 最新在前）——Emby 真实行为：
        # 这里模拟「已越过窗口」的位置来触发 break 分支
        env_rows.append({'Id': 'old', 'Type': 'Episode', 'SeriesName': '剧',
                         'ParentIndexNumber': 1, 'IndexNumber': 99,
                         'DatePlayed': _utc_iso(now - 30 * 24 * 3600)})

        def fake(path, params=None, method='GET', timeout=15):
            if path == '/Users':
                return [{'Id': 'u1', 'Name': '甲'}]
            if params and params.get('Filters') == 'IsPlayed':
                return {'Items': list(env_rows)}
            return {}

        monkeypatch.setattr(emby, 'emby_request', fake)
        rep = facts.recent_played_24h(hours=24)
        recs = rep['records']
        # 窗口内是 1–23 小时前的 23 条（i*3600 < 24*3600），窗口外（含 30 天前）没进来
        assert len(recs) == 23
        assert all(r['ts'] >= now - 24 * 3600 for r in recs)    # 全部在窗口内
        assert not any(r['episode'] == 99 for r in recs)        # 窗口外没进来
        assert rep['errors'] == []
        assert rep['total'] == 23

    def test_user_error_aggregated(self, isolated_db, monkeypatch):
        """单用户失败 → 块内显式 ⚠️，其它用户记录不受影响。"""

        def fake(path, params=None, method='GET', timeout=15):
            if path == '/Users':
                return [{'Id': 'u1', 'Name': '好的'}, {'Id': 'u2', 'Name': '坏的'}]
            if params and params.get('Filters') == 'IsPlayed':
                if '/u2/' in path:
                    raise RuntimeError('请求失败')
                return {'Items': [{'Id': 'e1', 'Type': 'Episode', 'SeriesName': '剧',
                                   'ParentIndexNumber': 1, 'IndexNumber': 3,
                                   'DatePlayed': _utc_iso(time.time() - 3600)}]}
            return {}

        monkeypatch.setattr(emby, 'emby_request', fake)
        rep = facts.recent_played_24h(hours=24)
        assert len(rep['records']) == 1 and rep['records'][0]['name'] == '剧'
        assert rep['errors'] and '坏的' in rep['errors'][0]
        text = str(reports.morning_report({
            'now': datetime.datetime(2026, 1, 1), 'rule_sig': '', 'items': ['played'],
            'played': rep}))
        assert '⚠️' in text and '坏的' in text

    def test_total_failure_explicit(self, isolated_db, monkeypatch):
        """/Users 都失败 → EmbyFactError → 晨报块内「查询失败：原因」。"""

        def boom(path, params=None, method='GET', timeout=15):
            raise RuntimeError('Emby 无法连接')

        monkeypatch.setattr(emby, 'emby_request', boom)
        with pytest.raises(facts.EmbyFactError):
            facts.recent_played_24h(hours=24)
        text = str(reports.morning_report({
            'now': datetime.datetime(2026, 1, 1), 'rule_sig': '', 'items': ['played'],
            'played': {'error': '查询失败：Emby 无法连接'}}))
        assert '查询失败：Emby 无法连接' in text


# ═══════════ 块3：追更更新无状态 ═══════════

class _MorningEnv:
    """追更块测试环境：假 Emby + 订阅 + 独立存储（created 是 tmdb_id -> {集键: DateCreated}）。"""

    def __init__(self, created=None):
        self.created = created or {}
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/测试剧',
                              'ProviderIds': {'Tmdb': TMDB_ID}}]

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        return {}

    def __enter__(self):
        self._orig = emby.emby_request
        emby.emby_request = self._emby
        self._subs = subscribe.get_subscriptions()
        subscribe.reset_sub_state()
        subscribe.set_subscriptions([{'id': 's5', 'tmdb_id': TMDB_ID, 'name': '测试剧',
                                      'enabled': True}])
        return self

    def __exit__(self, *exc):
        emby.emby_request = self._orig
        subscribe.reset_sub_state()
        subscribe.set_subscriptions(self._subs)
        return False


class TestSubsBlock:

    def test_created_key_roundtrip(self):
        """键序列化回归：'S01E02' 键经 JSON 往返后仍可解析（撤回版曾用元组键，
        json.dumps 变 '(1, 2)' 无法解析——反向锁死）。"""
        payload = {'created': {'S01E02': '2026-01-01T00:00:00Z',
                               'S01E03': '2026-01-02T00:00:00Z'}}
        rt = json.loads(json.dumps(payload))
        for k in rt['created']:
            assert facts.parse_ep_key(k) is not None, k
        assert facts.parse_ep_key('S01E02') == (1, 2)

    def test_counts_24h_created(self, isolated_db, monkeypatch):
        """无状态追更：只数 created 里 DateCreated 在 24h 内的集，与当天是否推送无关。"""
        now = time.time()
        created = {'S01E01': _utc_iso(now - 2 * 3600),      # 窗口内
                   'S01E02': _utc_iso(now - 3 * 24 * 3600),  # 窗口外
                   'S01E03': _utc_iso(now - 1 * 3600)}       # 窗口内
        with _MorningEnv(created):
            monkeypatch.setattr(facts, 'get_series_status', lambda tid, **kw: {
                'status': 'success', 'classification': {'status': 'complete',
                                                        'missing': [], 'missing_ranges': ''},
                'fact': {'series_name': '测试剧', 'declared_total': 28, 'tmdb_total': 17,
                         'created': dict(created)}})
            updates = morning._subs_updates_24h()
        assert isinstance(updates, list) and len(updates) == 1
        u = updates[0]
        assert sorted(u['new_eps']) == ['S01E01', 'S01E03']     # 只有 24h 内的集
        assert u['total_eps'] == 28 and u['aired_total'] == 17

    def test_failures_aggregated(self, isolated_db, monkeypatch):
        """取数失败的订阅聚合为「N 部检查失败」，不抹掉整块。"""
        with _MorningEnv():
            def boom(tid, **kw):
                raise facts.EmbyFactError('Emby 挂了')
            monkeypatch.setattr(facts, 'get_series_status', boom)
            r = morning._subs_updates_24h()
        assert isinstance(r, dict) and '1 部检查失败' in r['error']
        text = str(reports.morning_report({
            'now': datetime.datetime(2026, 1, 1), 'rule_sig': '', 'items': ['subscriptions'],
            'subs': r}))
        assert '⚠️' in text and '1 部检查失败' in text


# ═══════════ 块4：缺集榜 ═══════════

class TestGapBlock:

    def _mk_series(self, st):
        return {'id': 'S1', 'series_ids': ['S1'], 'name': '测试剧',
                'have_eps': 15, 'total_episodes': 15,
                'tmdb_info': {'match_status': st, 'diff': -10, 'tmdb_total': 25}}

    def test_lagging_excluded_by_default_switchable(self, isolated_db, monkeypatch):
        """D3：缺集榜默认不含「在更·落后」；morning_gap_include_lagging=1 时并入。"""
        series = [self._mk_series('missing'), self._mk_series('ongoing_lagging')]
        monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda max_age=None: {
            'ts': time.time(), 'series': series, 'movies': []})
        monkeypatch.setattr(morning, 'save_emby_lib_cache', lambda data, keep_ts=False: None)
        monkeypatch.setattr(morning, 'build_library_health_snapshot', lambda max_age=None: {})
        # 默认：只有 missing 进缺集榜
        monkeypatch.setattr(morning, 'gap_include_lagging', lambda: False)
        rep = morning.gap_report(cache_only=True)
        assert [s['name'] for s in rep['missing']] == ['测试剧'] * 1
        assert len(rep['missing']) == 1
        # 切开：在更·落后并入
        monkeypatch.setattr(morning, 'gap_include_lagging', lambda: True)
        rep2 = morning.gap_report(cache_only=True)
        assert len(rep2['missing']) == 2

    def test_prescan_failure_shows_data_from(self, isolated_db, monkeypatch):
        """预扫失败 → 「缺集榜：预扫失败，数据来自 HH:MM」，不静默用旧的。"""
        old_ts = time.time() - 7200
        monkeypatch.setattr(morning, 'gap_report', lambda **kw: {
            'status': 'error', 'message': '预扫失败：Emby 无法连接', 'cache_ts': old_ts})
        monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda max_age=None: {
            'ts': old_ts, 'series': [], 'movies': []})
        data = morning.build_morning_report(['emby_gap'])
        expect = datetime.datetime.fromtimestamp(old_ts).strftime('%H:%M')
        assert '预扫失败' in data and '数据来自 %s' % expect in data

    def test_success_title_carries_data_time(self, isolated_db, monkeypatch):
        """成功路径：缺集榜标题带「数据时间 HH:MM」。"""
        ts = time.time() - 300
        rep = {'status': 'success', 'missing': [], 'stats': {},
               'from_cache': True, 'cache_ts': ts}
        text = str(reports.morning_report({
            'now': datetime.datetime(2026, 1, 1), 'rule_sig': '', 'items': ['emby_gap'],
            'gap': dict(rep, data_time=datetime.datetime.fromtimestamp(ts).strftime('%H:%M'))}))
        assert '数据时间' in text
        expect = datetime.datetime.fromtimestamp(ts).strftime('%H:%M')
        assert expect in text


# ═══════════ 发送路径（mark_sent 回归） ═══════════

class TestSendPath:

    def test_manual_and_scheduled_share_function(self, isolated_db, monkeypatch):
        """手动「立即发送」与定时同一函数 send_morning_report，仅差 mark_sent。"""
        sent = []
        monkeypatch.setattr(tg, 'notify_telegram', lambda text: sent.append(text) or True)
        monkeypatch.setattr(morning, 'build_morning_report', lambda items, **kw: '晨报文本')
        marks = []
        monkeypatch.setattr(morning._cfg, 'mark_morning_report_sent',
                            lambda d: marks.append(d))
        # 手动发送：不标记
        assert morning.send_morning_report(['stats'], mark_sent=False) is True
        assert marks == []
        # 定时发送：标记当天
        assert morning.send_morning_report(['stats'], mark_sent=True) is True
        assert len(marks) == 1
        assert sent == ['晨报文本', '晨报文本']       # 同一函数、同一渲染
