# -*- coding: utf-8 -*-
"""S2 探索重塑回归（白皮书 §5.2，验收 A4：两段式加载）：

① 第一段占位字段契约：/api/explore 只读 TMDB，卡片带 in_emby/eps 占位；
   TMDB 失败直接报错不降级；
② 第二段 /api/explore/status：批量并发、错误卡与「未入库」显式区分、
   手动完结标记传入 classify（防撤回版「手动完结被打回缺集」漏洞）、上限 60；
③ 60 秒自动刷新仅在「页面可见 + 探索页激活」时触发（静态断言级）；
④ 探索不再依赖 _emby_index / library_snapshot（静态断言级）。

Emby / TMDB 全部假实现替换，不联网。
"""
import json
import os

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import emby, facts, morning, state, storage, tmdb

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


class _FakeTmdb:
    """替换 tmdb.Tmdb：discover 返回固定卡片，get /tv/{id} 交给外部可改的 hook。"""

    def __init__(self, discover=None):
        self.key = 'test-key'                 # action_explore 检查 t.key
        self.discover = discover or {'results': [
            {'id': 8888, 'name': '测试剧', 'first_air_date': '2026-01-01',
             'vote_average': 8.0, 'poster_path': '/p.jpg', 'media_type': 'tv'},
        ] + [{'id': 9000 + i, 'name': '剧%d' % i, 'first_air_date': '2026-01-01',
              'vote_average': 7.0, 'poster_path': '', 'media_type': 'tv'}
             for i in range(45)]           # 45 张 → 验证截断 40
        }
        self.tv_hook = None
        self.calls = 0
        self.hits = 0

    def get(self, path, ttl=0, **kw):
        self.calls += 1
        if path.startswith('/discover/') or path.startswith('/search/'):
            # 真实 TMDB 两页内容不同：按页码切片，page=1/2 交替请求
            pg = int(kw.get('page', 1) or 1)
            per = 20
            start = (pg - 1) * per
            return {'results': list(self.discover['results'][start:start + per]),
                    'total_pages': 100, 'total_results': len(self.discover['results'])}
        if path.startswith('/tv/'):
            if self.tv_hook:
                return self.tv_hook(path)
            return {'name': '测试剧', 'status': 'Ended',
                    'seasons': [{'season_number': 1, 'episode_count': 25}],
                    'last_episode_to_air': {'season_number': 1, 'episode_number': 25}}
        return {}

    def save(self):
        pass


class _EmbyEnv:
    def __init__(self, episodes=None, series_items=None):
        self.episodes = episodes if episodes is not None else _full({1: 25})
        self.series_items = series_items if series_items is not None else [
            {'Id': 'S1', 'Name': '测试剧', 'Path': '/media/local/测试剧',
             'ProviderIds': {'Tmdb': TMDB_ID}}]
        self._orig = None

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            return {'Items': list(self.episodes)}
        return {}

    def __enter__(self):
        self._orig = emby.emby_request
        emby.emby_request = self._emby
        return self

    def __exit__(self, *exc):
        emby.emby_request = self._orig


# ═══════════ ① 第一段：占位字段契约 ═══════════

class TestExploreCatalog:

    def _args(self, **kw):
        from app.routers.deps import Args
        base = dict(region='all', year='', sort='popularity', media='tv',
                    page=1, q='', genre='')
        base.update(kw)
        return Args(**base)

    def test_placeholder_fields_contract(self, isolated_db, monkeypatch):
        fake = _FakeTmdb()
        monkeypatch.setattr(tmdb, 'Tmdb', lambda: fake)
        out = tmdb.action_explore(self._args())
        assert out['status'] == 'success'
        assert len(out['cards']) == 40                    # 两页拼 40 张
        c = out['cards'][0]
        assert c['tmdb_id'] == '8888' and c['type'] == 'tv'
        assert c['in_emby'] is None and c['eps'] is None and c['eps_source'] is None
        assert c['in_local'] is False and c['in_share'] is False and c['emby_id'] is None
        assert c['poster'].startswith('https://image.tmdb.org')

    def test_tmdb_failure_errors_no_stale_fallback(self, isolated_db, monkeypatch):
        """TMDB 失败直接报错，不降级成旧缓存（§5.2）。"""
        class Boom(tmdb.Tmdb):
            def get(self, path, ttl=0, **kw):
                raise tmdb.TmdbError('无法访问 TMDB')

            def save(self):
                pass
        monkeypatch.setattr(tmdb, 'Tmdb', Boom)
        out = tmdb.action_explore(self._args())
        assert out['status'] == 'error' and 'TMDB' in out['message']
        assert 'cards' not in out

    def test_search_multi_persons_filtered(self, isolated_db, monkeypatch):
        fake = _FakeTmdb({'results': [
            {'id': 1, 'name': '某人', 'media_type': 'person'},
            {'id': 2, 'name': '测试剧', 'media_type': 'tv'},
        ]})
        monkeypatch.setattr(tmdb, 'Tmdb', lambda: fake)
        out = tmdb.action_explore(self._args(media='all', q='测试'))
        assert out['status'] == 'success'
        assert [c['tmdb_id'] for c in out['cards']] == ['2']     # person 被过滤


# ═══════════ ② 第二段：批量状态端点 ═══════════

class TestExploreStatusEndpoint:

    def test_batch_status_success_and_not_in_library(self, isolated_db, monkeypatch):
        from app.routers.library import api_explore_status
        with _EmbyEnv():
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=25))
            r = api_explore_status(ids='%s,9999' % TMDB_ID, media='tv')
        assert r['status'] == 'success' and r['count'] == 2 and r['data_time']
        by_id = {row.get('tmdb_id') or row['fact']['tmdb_id']: row for row in r['data']}
        # 已入库：classification 携带事实
        hit = by_id[TMDB_ID]
        assert hit['status'] == 'success'
        assert hit['classification']['status'] == 'complete'
        assert hit['fact']['have_total'] == 25 and hit['fact']['matched'] is True
        assert hit['data_time']
        # 未入库：matched=False，也是 success（不是 error）
        miss = by_id['9999']
        assert miss['status'] == 'success' and miss['fact']['matched'] is False

    def test_error_card_distinct_from_not_in_library(self, isolated_db):
        """Emby 断开 → 错误卡 {status:'error', message}，与「未入库」显式区分。"""
        from app.routers.library import api_explore_status
        with _EmbyEnv() as env:
            def boom(path, params=None, method='GET', timeout=15):
                raise RuntimeError('连接被拒绝')
            emby.emby_request = boom
            r = api_explore_status(ids=TMDB_ID, media='tv')
        assert r['status'] == 'success'                 # 批量本身成功
        row = r['data'][0]
        assert row['status'] == 'error' and '连接被拒绝' in row['message']
        assert row['tmdb_id'] == TMDB_ID

    def test_manual_done_passed_into_classify(self, isolated_db, monkeypatch):
        """手动完结标记必须传入 classify：防撤回版「手动完结被打回缺集」漏洞。"""
        from app.routers.library import api_explore_status
        storage.db_doc_put(storage.DOC_MANUAL_DONE, {TMDB_ID: {'name': '测试剧', 'ts': 1}})
        with _EmbyEnv():
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Returning Series', total=25))
            r = api_explore_status(ids=TMDB_ID, media='tv')
        row = r['data'][0]
        assert row['status'] == 'success'
        # 库内只有部分集 + 在更：没有手动完结会是 ongoing_lagging
        assert row['classification']['status'] == 'complete'
        assert row['classification']['manual_done'] is True

    def test_id_limit_60(self, isolated_db):
        """上限 60 个 ID：超出部分被截断（防恶意超长查询）。"""
        from app.routers.library import api_explore_status
        with _EmbyEnv() as env:
            env.series_items = []
            env.episodes = []
            ids = ','.join(str(100000 + i) for i in range(80))
            r = api_explore_status(ids=ids, media='tv')
        assert r['count'] == 60

    def test_invalid_media_rejected(self, isolated_db):
        from app.routers.library import api_explore_status
        r = api_explore_status(ids=TMDB_ID, media='book')
        assert r['status'] == 'error'

    def test_movie_batch(self, isolated_db, monkeypatch):
        from app.routers.library import api_explore_status
        with _EmbyEnv() as env:
            env.series_items = []
            env.episodes = []
            orig = env._emby

            def with_movie(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Movie':
                    return {'Items': [{'Id': 'M1', 'Name': '测试电影',
                                       'Path': '/media/share/movie1.strm',
                                       'ProviderIds': {'Tmdb': TMDB_ID}}]}
                return orig(path, params, method, timeout)
            emby.emby_request = with_movie
            r = api_explore_status(ids=TMDB_ID, media='movie')
        row = r['data'][0]
        assert row['status'] == 'success'
        assert row['fact']['in_share'] is True and row['fact']['in_local'] is False

    def test_concurrent_not_serial(self, isolated_db, monkeypatch):
        """第二段并发：4 张卡每卡 0.2s 慢查询，串行 0.8s，8 线程并发应显著更快。"""
        import time as _t
        from app.routers.library import api_explore_status
        with _EmbyEnv() as env:
            env.series_items = [{'Id': 'S%d' % i, 'Name': '剧%d' % i,
                                 'Path': '/media/local/剧%d' % i,
                                 'ProviderIds': {'Tmdb': str(1000 + i)}} for i in range(4)]
            monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: _tv_info('Ended', total=2))
            orig = env._emby

            def slow(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Episode':
                    _t.sleep(0.2)
                return orig(path, params, method, timeout)
            emby.emby_request = slow
            t0 = _t.monotonic()
            r = api_explore_status(ids=','.join(str(1000 + i) for i in range(4)), media='tv')
            el = _t.monotonic() - t0
        assert r['count'] == 4 and all(row['status'] == 'success' for row in r['data'])
        assert el < 0.6, '串行应约 0.8s（4×0.2），并发 8 线程应显著低于（实测 %.2fs）' % el


# ═══════════ ③④ 静态契约：前端自动刷新与依赖删除 ═══════════

class TestStaticContracts:
    HTML = open('static/index.html', encoding='utf-8').read()

    def test_auto_refresh_only_when_visible_and_active(self):
        assert 'setInterval(exploreAutoRefreshTick, 60000)' in self.HTML
        assert "document.hidden || document.visibilityState !== 'visible'" in self.HTML
        assert "window.__activeTab !== 'explore'" in self.HTML
        # 切走即停
        assert 'stopExploreAutoRefresh()' in self.HTML and 'ensureExploreAutoRefresh()' in self.HTML

    def test_two_phase_load_flow(self):
        assert "api('/api/explore/status?ids='" in self.HTML
        assert 'refreshExploreStatus(' in self.HTML and 'updateExploreCardBadge(' in self.HTML
        # 错误卡与未入库视觉分离：err 与 no 是不同徽标类
        assert 'badge-status.err' in self.HTML and "'err'" in self.HTML
        assert "'⚠ Emby 查询失败'" in self.HTML

    def test_explore_no_longer_reads_index_or_snapshot(self):
        import subprocess
        # static/index.html 从不直接读这些（后端口径），检查后端源文件
        tmdb_src = open('app/tmdb.py', encoding='utf-8').read()
        assert 'emby_library_index' not in tmdb_src
        assert '_load_emby_index_disk' not in tmdb_src
        assert 'load_library_snapshot' not in tmdb_src
        assert '_live_eps_cache' not in tmdb_src
