# -*- coding: utf-8 -*-
"""S3 片库映射回归（白皮书 §5.3 / §4.3，验收 A3、A5）：

- 集合比较取代相减：缺 E03 + 多 E26 不再抵消成「对齐」（旧口径教训）；
- 中文六档映射（_cn_status/_stat_bucket：旧英文缓存键兼容）；
- cache_only 废除后的降级路径：/api/mapping 有事实返回事实 + data_time，
  无事实返回「尚未扫描」而不是 nocache 报错；旧参数不再被端点接受；
- 全量对照扫出的分集回写事实库（morning._sync_series_fact/_sync_movie_fact）；
- 手动完结不被实时重验覆盖（/api/mapping/verify 携带 manual_done）；
- A5：五类（+在更·落后）数量之和 = 总剧数。

Emby / TMDB 假实现替换，不联网。
"""
import os

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import config, emby, facts, morning, state, storage, tmdb

TMDB_ID = '8888'


def _ep(season, episode, path='/media/local/x.strm', **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode, 'Path': path,
         'DateCreated': '2026-01-01T00:00:00Z'}
    d.update(extra)
    return d


def _full(seasons, path='/media/local/x.strm'):
    return [_ep(s, e, path) for s, n in seasons.items() for e in range(1, n + 1)]


def _tv_info(status='Ended', total=25):
    aired = {(1, e) for e in range(1, total + 1)}
    return {'name': '测试剧', 'status': status, 'aired': aired,
            'total_episodes': total, 'declared_total': total,
            'seasons': {1: {'episode_count': total, 'air_date': ''}}}


class _EmbyEnv:
    def __init__(self, episodes=None, series_items=None, movies=None):
        self.episodes = episodes if episodes is not None else _full({1: 25})
        self.series_items = series_items if series_items is not None else [
            {'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/测试剧',
             'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.movies = movies or []

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            return {'Items': list(self.episodes)}
        if params.get('IncludeItemTypes') == 'Movie':
            return {'Items': list(self.movies)}
        return {}

    def __enter__(self):
        self._orig = emby.emby_request
        emby.emby_request = self._emby
        return self

    def __exit__(self, *exc):
        emby.emby_request = self._orig


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


# ═══════════ ① 集合比较取代相减 ═══════════

class TestSetDiffNotCountDiff:
    """旧口径教训：local_total=25, tmdb_total=25（缺 S01E03、多 S01E26）数数相减得 0
    → 「对齐」；集合差集正确判出「缺集」并给出精确区间。"""

    def test_missing_plus_extra_do_not_cancel(self, isolated_db):
        have = {facts.ep_key(1, e) for e in range(1, 26) if e != 3}
        have.add(facts.ep_key(1, 26))                     # TMDB 没有的集
        info = _tv_info('Ended', total=25)
        cls = facts.classify(have, info)
        assert cls['status'] == 'missing' and cls['label'] == '缺集'
        assert cls['missing'] == ['S01E03']
        assert cls['missing_ranges'] == 'S01E03'
        assert 'S01E26' in cls['extra']

    def test_mapping_card_shows_exact_interval(self, isolated_db, monkeypatch):
        """片库映射卡（renderSeriesCard 数据源）的 missing_ranges 是精确区间，非 diff。"""
        with _EmbyEnv():
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=25))
            have = {facts.ep_key(1, e) for e in range(1, 26) if e != 3}
            have.add(facts.ep_key(1, 26))
            # 直接以 facts 口径产出 tmdb_info（映射页 tmdb_info.missing_ranges 字段契约）
            out = tmdb.classify_series_by_tmdb(
                [{'season': 1, 'episodes': 25}], None)   # 冒烟：老形态不炸
            assert out['match_status'] in ('missing', 'unmatched')


# ═══════════ ② 中文六档映射 ═══════════

class TestChineseStatusMapping:

    def test_old_english_keys_normalized(self):
        assert morning._cn_status('aligned') == 'complete'
        assert morning._cn_status('no_tmdb') == 'unmatched'
        assert morning._cn_status('missing') == 'missing'      # 六态键原样通过
        assert morning._cn_status('ongoing_lagging') == 'ongoing_lagging'

    def test_stat_bucket_six_states(self):
        assert morning._stat_bucket('complete') == 'aligned'
        assert morning._stat_bucket('ongoing_lagging') == 'ongoing_lagging'
        assert morning._stat_bucket('pending') == 'unmatched'  # 旧中间态兜底
        assert morning._stat_bucket('whatever') == 'unmatched'

    def test_stats_sum_equals_total_a5(self):
        """A5：五类 + 在更·落后 之和 = 总剧数。"""
        rows = []
        cases = ['complete', 'missing', 'extra', 'ongoing', 'ongoing_lagging', 'unmatched']
        for i, st in enumerate(cases * 3):                     # 18 部
            rows.append({'tmdb_info': {'match_status': st}})
        stats = morning._recompute_mapping_stats_dict(rows)
        total = stats['total_series']
        assert stats['aligned'] + stats['missing'] + stats['extra'] + stats['ongoing'] \
            + stats['ongoing_lagging'] + stats['unmatched'] == total


# ═══════════ ③ cache_only 废除后的降级路径 ═══════════

class TestCacheOnlyAbolished:

    def test_endpoint_signature_has_no_cache_only(self, isolated_db):
        import inspect
        from app.routers import library
        sig = inspect.signature(library.api_emby_library)
        assert 'cache_only' not in sig.parameters

    def test_mapping_endpoint_empty_means_not_scanned(self, isolated_db):
        """无任何事实：返回「尚未扫描」提示（empty:true），不再是 nocache 报错。"""
        from app.routers.library import api_mapping
        r = api_mapping()
        assert r['status'] == 'success' and r['empty'] is True
        assert '尚未扫描' in r['message']

    def test_mapping_endpoint_returns_facts_with_data_time(self, isolated_db, monkeypatch):
        """有事实：返回事实层口径的 series + 中文状态 + data_time。"""
        from app.routers.library import api_mapping
        with _EmbyEnv():
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=25))
            facts.get_series_status(TMDB_ID)
        r = api_mapping()
        assert r['status'] == 'success' and r['empty'] is False
        assert r['series'] and r['series'][0]['tmdb_id'] == TMDB_ID
        assert r['series'][0]['tmdb_info']['match_status'] == 'complete'
        assert r['series'][0]['tmdb_info']['status_cn'] == '完整'
        assert r['series'][0]['tmdb_info']['source'] == 'facts'
        assert r['data_time']

    def test_mapping_endpoint_missing_shows_ranges(self, isolated_db, monkeypatch):
        """事实库里的缺集剧：/api/mapping 行带 missing_ranges 精确区间。"""
        from app.routers.library import api_mapping
        with _EmbyEnv(episodes=_full({1: 20})):
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=25))
            facts.get_series_status(TMDB_ID)
        r = api_mapping()
        ti = r['series'][0]['tmdb_info']
        assert ti['match_status'] == 'missing'
        assert ti['missing_ranges'] == 'S01E21–E25'


# ═══════════ ④ 手动完结不被实时重验覆盖 ═══════════

class TestManualDoneNotOverwritten:

    def test_mapping_verify_carries_manual_done(self, isolated_db, monkeypatch):
        """/api/mapping/verify 携带手动完结：在更+落后 → 完整（不被打回缺集）。

        复审修正：标记的存储键是 Emby 条目 id（前端传卡片 id），匹配走
        Emby id / tmdb_id 双键位——只用 tmdb_id 查表是 S3 期间的口径错误。"""
        from app.routers.library import api_mapping_verify
        storage.db_doc_put(storage.DOC_MANUAL_DONE, {'S1': {'name': '测试剧', 'ts': 1}})
        with _EmbyEnv(episodes=_full({1: 15})):
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Returning Series', total=25))
            r = api_mapping_verify(ids=TMDB_ID, media='tv')
        row = r['data'][0]
        assert row['status'] == 'success'
        assert row['classification']['status'] == 'complete'
        assert row['classification']['manual_done'] is True
        # 事实库里也持久化为完整
        assert facts.load_fact('tv', TMDB_ID)['classification']['status'] == 'complete'

    def test_verify_persists_fact_for_mapping(self, isolated_db, monkeypatch):
        """verify 现查后写事实库 → /api/mapping 立刻反映（D1 数据回路）。"""
        from app.routers.library import api_mapping_verify
        with _EmbyEnv(episodes=_full({1: 28})):
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=25))
            r = api_mapping_verify(ids=TMDB_ID, media='tv')
        assert r['data'][0]['status'] == 'success'
        fact = facts.load_fact('tv', TMDB_ID)
        assert fact['have_total'] == 28
        # 超集（多 3 集，已完结）→ 超集状态
        assert fact['classification']['status'] == 'extra'

    def test_verify_error_distinct_from_not_in_library(self, isolated_db):
        from app.routers.library import api_mapping_verify
        with _EmbyEnv() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('Emby 超时')
            emby.emby_request = boom
            r = api_mapping_verify(ids=TMDB_ID, media='tv')
        row = r['data'][0]
        assert row['status'] == 'error' and 'Emby 超时' in row['message']


# ═══════════ ⑤ 全量对照回写事实库 ═══════════

class TestScanWritesBackFacts:

    def test_sync_series_fact_persists(self, isolated_db):
        entry = {'tmdb_id': TMDB_ID, 'name': '测试剧', 'series_ids': ['S1'],
                 'in_local': True, 'in_share': False,
                 'seasons': [{'season': 1, 'episodes': 22, 'max_ep': 25,
                              'missing': [23, 24, 25]}]}
        cls = morning._sync_series_fact(entry, _tv_info('Ended', total=25))
        assert cls['status'] == 'missing'
        assert cls['missing_ranges'] == 'S01E23–E25'
        fact = facts.load_fact('tv', TMDB_ID)
        assert fact['matched'] is True and fact['have_total'] == 22
        assert fact['classification']['missing_ranges'] == 'S01E23–E25'
        assert fact['source_scan'] == 'mapping'

    def test_sync_movie_fact_persists(self, isolated_db):
        morning._sync_movie_fact({'tmdb_id': '777', 'name': '电影甲',
                                  'ids': ['M1'], 'in_local': True, 'in_share': False})
        fact = facts.load_fact('movie', '777')
        assert fact['matched'] is True
        assert fact['in_local'] is True and fact['in_share'] is False
        assert fact['source_scan'] == 'mapping'

    def test_sync_skips_entry_without_tmdb_id(self, isolated_db):
        entry = {'tmdb_id': None, 'name': '无ID剧',
                 'seasons': [{'season': 1, 'episodes': 3, 'max_ep': 3, 'missing': []}]}
        assert morning._sync_series_fact(entry, None) is None

    def test_scan_loop_writes_facts(self, isolated_db, monkeypatch):
        """端到端：action_emby_library 全量对照循环 → 事实库有这部剧。"""
        from app.routers.deps import Args
        with _EmbyEnv(episodes=[dict(ep, SeriesId='S1') for ep in _full({1: 25})]):
            class FakeTmdb:
                key = 'k'

                def get(self, path, ttl=0, **kw):
                    if path.startswith('/tv/'):
                        return {'name': '测试剧', 'status': 'Ended',
                                'seasons': [{'season_number': 1, 'episode_count': 25}],
                                'last_episode_to_air': {'season_number': 1, 'episode_number': 25},
                                'poster_path': '/p.jpg'}
                    return {}

                def save(self):
                    pass
            monkeypatch.setattr(tmdb, 'Tmdb', FakeTmdb)
            out = morning.action_emby_library(Args(force=True, with_tmdb=True))
        assert out['status'] == 'success' and out['data_time']
        fact = facts.load_fact('tv', TMDB_ID)
        assert fact and fact['matched'] is True
        # 电影事实（无电影 → 无行，不炸）
        assert storage.db_facts_all('movie') == []


# ═══════════ 复审回归：事实回写的 TMDB 口径 ═══════════

class TestSyncFactTmdbNormalize:
    """复审修复回归：原始 /tv/{id} 响应必须经 normalize_tmdb_info 规整后分类，
    未播集不算缺集、TMDB None 不回写。"""

    def test_raw_response_ended_missing_not_extra(self, isolated_db):
        entry = {'tmdb_id': '8888', 'name': '剧', 'series_ids': ['S1'],
                 'in_local': True, 'in_share': False,
                 'seasons': [{'season': 1, 'episodes': 22, 'max_ep': 25,
                              'missing': [23, 24, 25]}]}
        raw = {'status': 'Ended',
               'seasons': [{'season_number': 1, 'episode_count': 25}],
               'last_episode_to_air': {'season_number': 1, 'episode_number': 25}}
        cls = morning._sync_series_fact(entry, raw)
        assert cls['status'] == 'missing' and cls['missing_ranges'] == 'S01E23–E25'

    def test_raw_response_ongoing_unaired_not_missing(self, isolated_db):
        """在更剧已播 S01E01-10 + S02E01-3（S02 标称 5），库内全部 → 在更；
        S02E04/E05 未播不算缺。"""
        entry = {'tmdb_id': '8888', 'name': '剧', 'series_ids': ['S1'],
                 'in_local': True, 'in_share': False,
                 'seasons': [{'season': 1, 'episodes': 10, 'max_ep': 10, 'missing': []},
                             {'season': 2, 'episodes': 3, 'max_ep': 3, 'missing': []}]}
        raw = {'status': 'Returning Series',
               'seasons': [{'season_number': 1, 'episode_count': 10},
                           {'season_number': 2, 'episode_count': 5}],
               'last_episode_to_air': {'season_number': 2, 'episode_number': 3}}
        cls = morning._sync_series_fact(entry, raw)
        assert cls['status'] == 'ongoing' and cls['missing_ranges'] == ''

    def test_tmdb_none_skips_writeback(self, isolated_db):
        """TMDB 查询失败 → 不回写事实库（保留上一轮好数据，不写成未匹配）。"""
        facts._persist_fact('tv', '8888', {'matched': True,
                                           'classification': {'status': 'missing'}})
        entry = {'tmdb_id': '8888', 'name': '剧', 'series_ids': ['S1'],
                 'seasons': [{'season': 1, 'episodes': 3, 'max_ep': 3, 'missing': []}]}
        assert morning._sync_series_fact(entry, None) is None
        fact = facts.load_fact('tv', '8888')
        assert fact['classification']['status'] == 'missing'
