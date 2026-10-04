# -*- coding: utf-8 -*-
"""S1 事实层回归（白皮书 §5.1 / §5.3，验收 A3 口径一致底座）：

- 集合差集与区间格式化（'S01E26–E28'）；
- S00（特别篇）单独标记、不计缺集；
- 手动完结只在 facts.classify 内应用（全仓唯一处）；
- 错误上抛不冒充空（Emby 不可达 / Key 为空 → EmbyFactError）；
- payload JSON 往返后分集键保持 'S01E02' 字符串（撤回版元组键 bug 的回归）；
- get_series_status 重复调用同一条事实只写一次库；
- classify_series_by_tmdb 薄封装口径与中文六态（含旧 fake 形态兼容）。

Emby 用假实现替换（沿用 tests/test_subscriptions.py 的 _Env 手法），不联网。
"""
import json
import os

os.environ.setdefault('WEB_PASSWORD', 's1-test')

import pytest

from app import config, emby, facts, media, state, storage, subscribe, tmdb

TMDB_ID = '8888'


def _ep(season, episode, path='/media/local/x.strm', **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode, 'Path': path,
         'DateCreated': '2026-01-01T00:00:00Z'}
    d.update(extra)
    return d


def _full(seasons, path='/media/local/x.strm'):
    return [_ep(s, e, path) for s, n in seasons.items() for e in range(1, n + 1)]


def _tv_info(status='Ended', seasons=(1,), total=25):
    """构造 tmdb._tmdb_series_info 形态（aired 为 (季,集) 集合）。"""
    aired = set()
    for sn in seasons:
        aired |= {(sn, e) for e in range(1, total + 1)}
    return {'name': '测试剧', 'status': status, 'aired': aired,
            'total_episodes': total * len(seasons), 'declared_total': total * len(seasons),
            'seasons': {sn: {'episode_count': total, 'air_date': ''} for sn in seasons}}


class _Env:
    """假 Emby：series_items / episodes 可在 with 块内动态改；结束时全部还原。"""

    def __init__(self):
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/测试剧',
                              'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.episodes = []

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            items = list(self.series_items)
            if params.get('AnyProviderIdEquals'):
                # 复刻 PR12 修掉的缺陷面：正式服务端会忽略过滤参数返回全库 →
                # facts._series_items_exact 必须逐条校验 ProviderIds
                pass
            return {'Items': items}
        if params.get('IncludeItemTypes') == 'Episode':
            return {'Items': list(self.episodes)}
        return {}

    def __enter__(self):
        self._orig = (emby.emby_request, state.SUB_STORE)
        emby.emby_request = self._emby
        state.SUB_STORE = 's1_facts_test'
        subscribe.reset_sub_state()
        return self

    def __exit__(self, *exc):
        (emby.emby_request, state.SUB_STORE) = self._orig
        media.invalidate()


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """media_facts 表落到独立 STATE_DIR，库根与 Emby 路径映射对齐 /media/{local,share}；
    测试结束关闭连接。"""
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    monkeypatch.setattr(state, 'L_ROOT', tmp_path / 'local')
    monkeypatch.setattr(state, 'S_ROOT', tmp_path / 'share')
    monkeypatch.setattr(state, 'EMBY_PATHS', state.emby_path_map('/media/local', '/media/share'))
    yield sd
    storage.close_all()


# ═══════════ ① 集合差集与区间格式化 ═══════════

class TestClassifyAndRanges:

    def test_missing_ranges_exact_interval(self):
        have = {facts.ep_key(1, e) for e in range(1, 26)}          # E01–25
        have |= {facts.ep_key(1, 29)}
        info = _tv_info(status='Ended', total=30)                  # 已播 30 → 缺 E26–28、E30
        cls = facts.classify(have, info)
        assert cls['status'] == 'missing' and cls['label'] == '缺集'
        assert cls['missing_ranges'] == 'S01E26–E28, S01E30'
        assert cls['missing'] == ['S01E26', 'S01E27', 'S01E28', 'S01E30']

    def test_set_diff_not_count_diff(self):
        """集合相减：缺 2 集 + 多 1 集（未完结 TMDB 没有的集）不能互相抵消。"""
        have = {facts.ep_key(1, e) for e in range(1, 26)}          # E01–25
        have.add('S01E90')                                         # TMDB 没有
        info = _tv_info(status='Ended', total=27)                  # 已播 27 → 缺 E26–27
        cls = facts.classify(have, info)
        assert cls['status'] == 'missing'
        assert cls['missing'] == ['S01E26', 'S01E27']
        assert 'S01E90' in cls['extra']

    def test_extra_only_when_ended(self):
        have = {facts.ep_key(1, e) for e in range(1, 20)}
        info = _tv_info(status='Ended', total=15)
        assert facts.classify(have, info)['status'] == 'extra'
        # 同样的超集，在更剧不算超集（在更且无缺集优先）
        info_now = _tv_info(status='Returning Series', total=15)
        assert facts.classify(have, info_now)['status'] == 'ongoing'

    def test_six_statuses(self):
        full25 = {facts.ep_key(1, e) for e in range(1, 26)}
        # 完整：完结且 aired ⊆ have
        assert facts.classify(full25, _tv_info('Ended', total=25))['status'] == 'complete'
        # 在更：Returning 且无缺
        assert facts.classify(full25, _tv_info('Returning Series', total=25))['status'] == 'ongoing'
        # 在更·落后：Returning 且有缺（D3 单独一档）
        lag = facts.classify({facts.ep_key(1, e) for e in range(1, 20)}, _tv_info('Returning Series', total=25))
        assert lag['status'] == 'ongoing_lagging' and lag['label'] == '在更·落后'
        # 缺集：完结且有缺
        assert facts.classify(full25 - {'S01E07'}, _tv_info('Ended', total=25))['status'] == 'missing'
        # 超集：完结且多集
        sup = full25 | {'S01E99'}
        assert facts.classify(sup, _tv_info('Ended', total=25))['status'] == 'extra'
        # 未匹配：无 TMDB 数据
        assert facts.classify(full25, None)['status'] == 'unmatched'

    def test_labels_are_chinese(self):
        full25 = {facts.ep_key(1, e) for e in range(1, 26)}
        cases = [('complete', '完整'), ('missing', '缺集'), ('extra', '超集'),
                 ('ongoing', '在更'), ('ongoing_lagging', '在更·落后'), ('unmatched', '未匹配')]
        for status, label in cases:
            if status == 'complete':
                cls = facts.classify(full25, _tv_info('Ended', total=25))
            elif status == 'missing':
                cls = facts.classify(full25 - {'S01E07'}, _tv_info('Ended', total=25))
            elif status == 'extra':
                cls = facts.classify(full25 | {'S01E99'}, _tv_info('Ended', total=25))
            elif status == 'ongoing':
                cls = facts.classify(full25, _tv_info('Returning Series', total=25))
            elif status == 'ongoing_lagging':
                cls = facts.classify(full25 - {'S01E07'}, _tv_info('Returning Series', total=25))
            else:
                cls = facts.classify(full25, None)
            assert cls['label'] == label and facts.STATUS_LABELS[cls['status']] == label


# ═══════════ ② S00 不计缺集 ═══════════

class TestSpecialSeason:

    def test_s00_marked_not_counted(self, isolated_db):
        with _Env() as env:
            env.episodes = [_ep(0, 1), _ep(0, 2), *_full({1: 25})]
            eps = facts.series_episodes(['S1'])
            assert eps['special'] == {'S00E01', 'S00E02'}
            assert 'S00E01' not in eps['episodes']
            info = _tv_info('Ended', total=25)
            cls = facts.classify(eps['episodes'], info)
            # S00 即使 TMDB 没有，也不能把剧打成超集/缺集
            assert cls['status'] == 'complete'

    def test_s00_not_missing_when_tmdb_lacks_it(self):
        """aired−have 差集不因 S00 起意：S00 在 have 里也不进缺集/超集计算。"""
        have = {facts.ep_key(1, e) for e in range(1, 26)} | {'S00E01'}
        info = _tv_info('Ended', total=25)
        cls = facts.classify(have, info)
        assert cls['status'] == 'complete' and 'S00E01' not in cls['extra']


# ═══════════ ③ 手动完结只在 classify 生效 ═══════════

class TestManualDoneOnlyInClassify:

    def test_manual_done_forces_complete(self):
        lagging = {facts.ep_key(1, e) for e in range(1, 15)}
        info = _tv_info('Returning Series', total=25)
        cls = facts.classify(lagging, info, manual_done=True)
        assert cls['status'] == 'complete' and cls['label'] == '完整'
        assert cls['manual_done'] is True
        # 同一份数据不传 manual_done → 在更·落后（判定只发生在 classify 内）
        assert facts.classify(lagging, info)['status'] == 'ongoing_lagging'

    def test_apply_manual_done_uses_classify_key(self, isolated_db):
        """morning._apply_manual_done_to_series 是历史缓存行的等价展示修正，
        产出的状态键必须与新口径一致（complete），_md 标记保留。"""
        from app import morning
        storage.db_doc_put(storage.DOC_MANUAL_DONE, {'S1': {'name': '测试剧', 'ts': 1}})
        rows = morning._apply_manual_done_to_series([
            {'id': 'S1', 'series_ids': ['S1'], 'name': '测试剧', 'have_eps': 15,
             'tmdb_info': {'match_status': 'ongoing', 'diff': -10}}])
        ti = rows[0]['tmdb_info']
        assert ti['match_status'] == 'complete' and ti['status_cn'] == '完整'
        assert rows[0]['_md'] is True
        # 新口径下旧英文键 aligned 也能被识别（混合代际缓存）
        rows2 = morning._apply_manual_done_to_series([
            {'id': 'S1', 'series_ids': ['S1'], 'tmdb_info': {'match_status': 'missing'}}])
        assert rows2[0]['tmdb_info']['match_status'] == 'complete'


# ═══════════ ④ 错误上抛不冒充空 ═══════════

class TestErrorsPropagate:

    def test_emby_down_raises_not_empty(self, isolated_db):
        with _Env() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('连接被拒绝')
            emby.emby_request = boom
            with pytest.raises(facts.EmbyFactError):
                facts.find_series(TMDB_ID)
            with pytest.raises(facts.EmbyFactError):
                facts.series_episodes(['S1'])
            with pytest.raises(facts.EmbyFactError):
                facts.movie_present(TMDB_ID)
            with pytest.raises(facts.EmbyFactError):
                facts.get_series_status(TMDB_ID)

    def test_empty_key_raises(self, isolated_db):
        with _Env() as env:
            def no_key(path, params=None, method='GET', timeout=15):
                if not state.EMBY_KEY:
                    raise RuntimeError('未配置 EMBY_KEY')
                return {}
            emby.emby_request = no_key
            old = state.EMBY_KEY
            state.EMBY_KEY = ''
            try:
                with pytest.raises(facts.EmbyFactError):
                    facts.find_series(TMDB_ID)
            finally:
                state.EMBY_KEY = old

    def test_all_layers_ok_but_no_match_is_honest_empty(self, isolated_db):
        """查询全部成功但没有匹配 → 诚实的「未入库」([])，不抛错。"""
        with _Env() as env:
            env.series_items = [{'Id': 'S9', 'Name': '别的剧', 'Path': '/media/local/别的剧',
                                 'ProviderIds': {'Tmdb': '9999'}}]
            assert facts.find_series(TMDB_ID) == []

    def test_refresh_screen_facts_reports_error_explicitly(self, isolated_db):
        with _Env() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('Emby 超时')
            emby.emby_request = boom
            out = facts.refresh_screen_facts([{'tmdb_id': TMDB_ID, 'type': 'tv'}])
            assert len(out) == 1
            assert out[0]['status'] == 'error' and 'Emby 超时' in out[0]['reason']


# ═══════════ ⑤ payload 往返键保持 'S01E02' ═══════════

class TestPayloadRoundTrip:

    def test_keys_survive_json_roundtrip(self, isolated_db):
        with _Env() as env:
            env.episodes = _full({1: 3})
            eps = facts.series_episodes(['S1'])
            keys = sorted(eps['episodes'])
            assert keys == ['S01E01', 'S01E02', 'S01E03']
            # 模拟落盘 JSON 往返：字符串键不能变成元组/列表
            rt = json.loads(json.dumps(sorted(eps['episodes'])))
            assert rt == keys and all(isinstance(k, str) for k in rt)

    def test_get_series_status_persists_and_reads_back(self, isolated_db):
        with _Env() as env:
            env.episodes = _full({1: 25})
            out = facts.get_series_status(TMDB_ID, tmdb_info=_tv_info('Ended', total=25))
            assert out['classification']['status'] == 'complete'
            row = facts.load_fact('tv', TMDB_ID)
            assert row['episodes'] == sorted(out['fact']['episodes'])
            assert all(isinstance(k, str) and k.startswith('S') for k in row['episodes'])
            assert row['classification']['missing_ranges'] == ''
            assert row['updated_at'] > 0

    def test_created_dates_recorded_for_morning(self, isolated_db):
        with _Env() as env:
            env.episodes = _full({1: 2})
            eps = facts.series_episodes(['S1'])
            assert eps['created']['S01E01'] == '2026-01-01T00:00:00Z'


# ═══════════ ⑥ 重复调用只写一次库 ═══════════

class TestSingleWrite:

    def test_get_series_status_writes_once(self, isolated_db, monkeypatch):
        calls = {'n': 0}
        real_put = storage.db_fact_put

        def counting_put(*a, **kw):
            calls['n'] += 1
            return real_put(*a, **kw)

        monkeypatch.setattr(storage, 'db_fact_put', counting_put)
        with _Env() as env:
            env.episodes = _full({1: 25})
            out = facts.get_series_status(TMDB_ID, tmdb_info=_tv_info('Ended', total=25))
            assert calls['n'] == 1          # 取数 + classify + 落盘，一次写入
            assert calls['n'] == 1
            # 再取一次（新事实）→ 又恰好一次写入，且 updated_at 前进
            before = facts.load_fact('tv', TMDB_ID)['updated_at']
            out2 = facts.get_series_status(TMDB_ID, tmdb_info=_tv_info('Ended', total=25))
            assert calls['n'] == 2
            assert facts.load_fact('tv', TMDB_ID)['updated_at'] >= before
            assert out2['classification']['status'] == 'complete'

    def test_refresh_screen_facts_concurrent(self, isolated_db, monkeypatch):
        """并发编排：多卡同时重验（撤回版串行的回归）。TMDB 查询打桩，
        只测 Emby 并发路径。"""
        import time as _t
        with _Env() as env:
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=2))
            env.series_items = [{'Id': 'S%d' % i, 'Name': '剧%d' % i,
                                 'Path': '/media/local/剧%d' % i,
                                 'ProviderIds': {'Tmdb': str(1000 + i)}} for i in range(4)]

            def slow(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Episode':
                    _t.sleep(0.2)            # 每部剧的分集查询慢 0.2s
                return {'Items': _full({1: 2})}
            emby.emby_request = slow
            items = [{'tmdb_id': str(1000 + i), 'type': 'tv'} for i in range(4)]
            t0 = _t.monotonic()
            out = facts.refresh_screen_facts(items)
            el = _t.monotonic() - t0
            assert len(out) == 4 and all(o['status'] == 'success' for o in out)
            assert el < 0.6, '串行应约 0.8s（4×0.2），并发 4 线程应显著低于该值（实测 %.2fs）' % el


# ═══════════ ⑦ find_series 三层兜底与薄封装口径 ═══════════

class TestFindSeriesLayers:

    def test_exact_layer_validates_provider_ids(self, isolated_db):
        """服务端忽略 AnyProviderIdEquals 返回全库（PR12 修的误命中缺陷）→
        facts 第一层必须逐条校验 ProviderIds.Tmdb。"""
        with _Env() as env:
            env.series_items = [
                {'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/a', 'ProviderIds': {'Tmdb': TMDB_ID}},
                {'Id': 'S2', 'Name': '冒名剧', 'Path': '/media/local/b', 'ProviderIds': {'Tmdb': '9999'}},
            ]
            assert facts.series_ids_by_tmdb(TMDB_ID) == ['S1']

    def test_fallback_to_dirname(self, isolated_db, monkeypatch):
        """ProviderIds 缺失 → 目录名 {tmdb-xxx} 磁盘兜底（上游 TgtoDrive 命名）。
        磁盘索引键是容器内真实路径（L_ROOT 下），Emby Path 经 to_container 转换后比对。"""
        with _Env() as env:
            env.series_items = [
                {'Id': 'S3', 'Name': '无刮削剧', 'Path': '/media/local/tmdb-%s' % TMDB_ID,
                 'ProviderIds': {}},
                {'Id': 'S4', 'Name': '别的剧', 'Path': '/media/local/tmdb-7777', 'ProviderIds': {}},
            ]
            from app import lib
            disk_keys = {'%s\\tmdb-%s' % (state.L_ROOT, TMDB_ID) if os.name == 'nt'
                         else '%s/tmdb-%s' % (state.L_ROOT, TMDB_ID): TMDB_ID}
            monkeypatch.setattr(lib, '_disk_tmdb_lookup', lambda: dict(disk_keys))
            assert facts.series_ids_by_tmdb(TMDB_ID) == ['S3']

    def test_movie_present_by_lib(self, isolated_db):
        with _Env() as env:
            env.series_items = []
            env.episodes = []
            orig = env._emby

            def with_movies(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Movie':
                    return {'Items': [
                        {'Id': 'M1', 'Name': '测试电影', 'Path': '/media/local/movie1.strm',
                         'ProviderIds': {'Tmdb': TMDB_ID}},
                        {'Id': 'M2', 'Name': '测试电影', 'Path': '/media/share/movie1.strm',
                         'ProviderIds': {'Tmdb': TMDB_ID}},
                    ]}
                return orig(path, params, method, timeout)
            emby.emby_request = with_movies
            r = facts.movie_present(TMDB_ID)
            assert r['matched'] is True and r['in_local'] is True and r['in_share'] is True
            assert r['movie_ids'] == ['M1', 'M2']

    def test_classify_wrapper_chinese_statuses(self, isolated_db):
        """tmdb.classify_series_by_tmdb 薄封装：中文六态 + fake 形态兼容 +
        集合差集（不数集数抵消）。"""
        info = {'status': 'Ended',
                'seasons': [{'season_number': 1, 'episode_count': 30}],
                'last_episode_to_air': {'season_number': 1, 'episode_number': 30}}
        got = tmdb.classify_series_by_tmdb([{'season': 1, 'episodes': 25}], info)
        assert got['match_status'] == 'missing' and got['status_cn'] == '缺集'
        assert got['missing_ranges'] == 'S01E26–E30'
        assert got['tmdb_total'] == 30 and got['local_total'] == 25 and got['diff'] == -5
        # 缺 1 集 + 另一季多 1 集：集合差集不抵消（旧数数口径会得出 aligned）
        info2 = {'status': 'Ended',
                 'seasons': [{'season_number': 1, 'episode_count': 26},
                             {'season_number': 2, 'episode_count': 10}],
                 'last_episode_to_air': {'season_number': 2, 'episode_number': 10}}
        got2 = tmdb.classify_series_by_tmdb(
            [{'season': 1, 'episodes': 25}, {'season': 2, 'episodes': 11}], info2)
        assert got2['match_status'] == 'missing' and got2['status_cn'] == '缺集'
        assert got2['missing_ranges'] == 'S01E26'
        assert got2['extra_total'] == 1
        # 无 TMDB → 未匹配（中文）
        got3 = tmdb.classify_series_by_tmdb([{'season': 1, 'episodes': 3}], None)
        assert got3['match_status'] == 'unmatched' and got3['status_cn'] == '未匹配'
        # fake 形态（晨报 _resync 拼出的缓存结构，无 aired 键）也能分类
        fake = {'status': 'Returning Series',
                'seasons': [{'season_number': 1, 'episode_count': 20}]}
        got4 = tmdb.classify_series_by_tmdb([{'season': 1, 'episodes': 15}], fake)
        assert got4['match_status'] == 'ongoing_lagging' and got4['status_cn'] == '在更·落后'

    def test_morning_stats_bucket_mapping(self):
        """晨报统计桶：新六态归进既有桶键，在更·落后单独一桶（D3）。"""
        from app import morning
        assert morning._stat_bucket('complete') == 'aligned'
        assert morning._stat_bucket('aligned') == 'aligned'      # 旧缓存键兼容
        assert morning._stat_bucket('ongoing_lagging') == 'ongoing_lagging'
        assert morning._stat_bucket('missing') == 'missing'
        assert morning._stat_bucket('unmatched') == 'unmatched'
        assert morning._cn_status('no_tmdb') == 'unmatched'
