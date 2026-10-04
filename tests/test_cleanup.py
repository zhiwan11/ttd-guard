# -*- coding: utf-8 -*-
"""S7 清理收口回归（白皮书 :326 / §5.9，验收 A10、A11）：

- 缓存收敛后各读方行为：探索/映射/统计/治理在删层后仍有正确数据路径；
- 免登录开关（D5 默认关）：默认拒绝、web_no_auth=1 且空密码放行、
  有密码时开关无效、空密码可启动；
- A10：app/ 内 except ImportError=0、_eng(=0（grep 级静态断言）；
- A11：红线回归文件存在且被本次全量运行覆盖（test_engine/test_safety 全绿由门禁保证）。
"""
import os
import re
from pathlib import Path

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import config as _cfg, emby, facts, morning, state, stats, storage
from app.routers import deps

REPO = Path(__file__).resolve().parent.parent


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


# ═══════════ 缓存收敛后的读方行为 ═══════════

class TestRemovedCachesGone:

    def test_removed_layers_absent_from_state(self):
        """被删的缓存层不再存在于 state 模块（残留即回归）。"""
        for name in ('_emby_index_cache', '_emby_index_refresh_lock', '_live_eps_cache',
                     '_live_eps_lock', '_strm_count_cache', '_strm_count_refreshing'):
            assert not hasattr(state, name), name

    def test_removed_stores_absent_from_morning(self):
        for name in ('_save_overview_disk', '_load_overview_disk',
                     'save_library_snapshot', 'load_library_snapshot',
                     'refresh_library_snapshot_background', 'get_tmdb_scan_progress'):
            assert not hasattr(morning, name), name

    def test_single_persistent_layer_for_mapping(self, isolated_db, monkeypatch):
        """映射数据只有一层持久存储（DOC_EMBY_LIB）：
        overview 构建写入后 read_emby_lib_cache 能读到同一份。"""
        monkeypatch.setattr(emby, 'emby_request',
                            lambda path, params=None, method='GET', timeout=15: {'Items': []})
        monkeypatch.setattr(morning.lib, '_disk_tmdb_lookup', lambda: {})
        monkeypatch.setattr(morning.lib, '_disk_eps_by_tmdb', lambda: {})
        out = morning.emby_library_overview(force=True)
        assert 'series' in out and 'movies' in out
        cached = morning.read_emby_lib_cache(max_age=None)
        assert cached is not None and cached['ts'] == out.get('ts', cached['ts'])

    def test_lib_get_always_fresh(self, isolated_db):
        """lib._get_lib 现扫磁盘：写入新 STRM 后立即可见（30 秒缓存已删）。"""
        (state.L_ROOT / '现扫剧 (2020)').mkdir(parents=True, exist_ok=True)
        (state.L_ROOT / '现扫剧 (2020)').mkdir(parents=True, exist_ok=True)
        season = state.L_ROOT / '现扫剧 (2020)' / 'Season 01'
        season.mkdir(exist_ok=True)
        before = lib_lib_keys()
        (season / 'E01.1080p.strm').write_text('http://x', encoding='utf-8')
        after = lib_lib_keys()
        assert len(after) == len(before) + 1       # 现扫立即看到新文件


def lib_lib_keys():
    from app import lib as _lib
    return set(_lib._get_lib(state.L_ROOT).meta.keys())


class TestReadersStillWork:

    def test_stats_single_layer_consistent(self, isolated_db):
        """统计缓存单层：_recompute_all_stats 后 _get_strm_counts 与统计一致。"""
        (state.L_ROOT / '国产剧').mkdir(parents=True, exist_ok=True)
        (state.L_ROOT / '国产剧' / 'a.strm').write_text('http://x', encoding='utf-8')
        stats._recompute_all_stats()
        l, s = stats._get_strm_counts()
        assert l == 1 and s == 0
        data = state._lib_stats_cache['data']
        assert data['local_total'] == 1            # 同一份缓存，无第二套数字

    def test_unified_health_reads_persistent_layer(self, isolated_db, monkeypatch):
        """library_snapshot 删除后 unified_health 直读 emby_lib_cache 仍可用。"""
        storage.db_doc_put(storage.DOC_EMBY_LIB, {
            'ts': 100.0, 'series': [{'name': '剧', 'have_eps': 5}],
            'movies': []})
        snap = morning.unified_health(max_age=None)
        assert snap['series'][0]['name'] == '剧'
        assert snap['episode_total'] == 5

    def test_facts_layer_unaffected(self, isolated_db, monkeypatch):
        """事实层 media_facts（保留项）读写正常。"""
        monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: None)
        monkeypatch.setattr(emby, 'emby_request',
                            lambda path, params=None, method='GET', timeout=15: {'Items': []})
        out = facts.get_series_status('424242')
        assert out['fact']['matched'] is False
        assert facts.load_fact('tv', '424242')['matched'] is False


# ═══════════ 免登录开关（D5 默认关） ═══════════

class TestNoAuthSwitch:

    def test_default_off_with_password(self, isolated_db, monkeypatch):
        """/api/config 需要鉴权（默认有密码）→ 401/200 分明；开关不影响有密码场景。"""
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        assert c.get('/api/config').status_code == 401
        assert c.get('/api/config', auth=('admin', 'test-pass-123')).status_code == 200

    def test_no_auth_mode_requires_empty_password(self, isolated_db, monkeypatch):
        """_no_auth_mode：有密码时恒 False（开关无效）；空密码 + web_no_auth=1 才 True。"""
        monkeypatch.setattr(deps, 'WEB_PASSWORD', 'x')
        monkeypatch.setattr(_cfg, 'load_config', lambda: {'web_no_auth': '1'})
        assert deps._no_auth_mode() is False
        monkeypatch.setattr(deps, 'WEB_PASSWORD', '')
        assert deps._no_auth_mode() is True
        monkeypatch.setattr(_cfg, 'load_config', lambda: {'web_no_auth': '0'})
        assert deps._no_auth_mode() is False
        monkeypatch.setattr(deps, 'ALLOW_NO_AUTH', True)   # 旧环境变量路径仍兼容
        assert deps._no_auth_mode() is True

    def test_empty_password_allows_startup(self):
        """空密码可启动：deps 导入不 SystemExit（本测试进程即空密码可启动的证明——
        conftest 统一设了 WEB_PASSWORD；这里用子进程在隔离环境复验）。"""
        import subprocess, sys, tempfile
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, '电影'), exist_ok=True)
        env = dict(os.environ)
        # PYTHONPATH 追加而非覆盖：conftest 的 fcntl 垫片目录已在里面（Windows 子进程必需）
        pp = os.environ.get('PYTHONPATH', '')
        env.update({'WEB_PASSWORD': '', 'ALLOW_NO_AUTH': '',
                    'CLOUD_L_ROOT': tmp,
                    'PYTHONPATH': (str(REPO) + os.pathsep + pp) if pp else str(REPO)})
        r = subprocess.run(
            [sys.executable, '-c',
             "import sys; sys.path.insert(0, %r)\nimport app.main\nprint('IMPORTED_OK')\n" % str(REPO)],
            env=env, capture_output=True, text=True, timeout=90)
        assert r.returncode == 0
        assert 'IMPORTED_OK' in r.stdout

    def test_auth_endpoint_rejects_wrong_password_even_with_switch(self, isolated_db, monkeypatch):
        """开关开着但配置里有密码 → 仍要求登录（双重条件：空密码才生效）。"""
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        monkeypatch.setattr(deps, 'WEB_PASSWORD', 'real-pass')
        monkeypatch.setattr(_cfg, 'load_config', lambda: {'web_no_auth': '1'})
        c = TestClient(main_app)
        assert c.get('/api/config').status_code == 401
        assert c.get('/api/config', auth=('admin', 'real-pass')).status_code == 200


# ═══════════ A10：grep 级验收 ═══════════

class TestA10:
    """全仓搜索口径（§5.8/:389）：app/ 内 except ImportError=0 且 _eng(=0；
    tests/ 与 scripts/ 不在范围。"""

    def test_no_import_error_guard_in_app(self):
        hits = []
        for py in (REPO / 'app').rglob('*.py'):
            for i, line in enumerate(py.read_text(encoding='utf-8').splitlines(), 1):
                if 'except ImportError' in line:
                    hits.append('%s:%d' % (py.relative_to(REPO), i))
        assert hits == []

    def test_no_eng_calls_in_app(self):
        hits = []
        for py in (REPO / 'app').rglob('*.py'):
            for i, line in enumerate(py.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'\b_eng\(', line):
                    hits.append('%s:%d' % (py.relative_to(REPO), i))
        assert hits == []

    def test_engine_is_thin_shell(self):
        """engine.py 无重导出：只保留 ACTIONS 注册表与 __getattr__ 转发壳。"""
        src = (REPO / 'app' / 'engine.py').read_text(encoding='utf-8')
        assert 'ACTIONS' in src
        assert '__getattr__' in src                 # 兼容转发壳
        assert '_eng(' not in src


# ═══════════ A11：红线回归在位 ═══════════

class TestA11:
    """K1–K5 红线回归：test_engine.py / test_safety.py 保留且纳入全量运行
    （文件在位 + 关键 7 维/安全用例名未被删；全绿由门禁 pytest 保证）。"""

    def test_redline_test_files_present(self):
        assert (REPO / 'tests' / 'test_engine.py').exists()
        assert (REPO / 'tests' / 'test_safety.py').exists()

    def test_quality_rules_cases_still_covered(self):
        eng = (REPO / 'tests' / 'test_engine.py').read_text(encoding='utf-8')
        safety = (REPO / 'tests' / 'test_safety.py').read_text(encoding='utf-8')
        # 7 维规则与安全边界的代表性用例必须仍在
        for kw in ('quality', 'rule_sig'):
            assert kw in eng.lower() or kw in safety.lower()
        assert 'plan' in safety.lower() and 'confirm' in safety.lower()


class TestManualDoneKeyConsistency:
    """复审修复回归：完结标记存储键是 Emby 条目 id；应用点按 Emby id / tmdb_id
    双键位匹配（只用 tmdb_id 查表会让现有用户标记永远失效）。"""

    def test_manual_done_for_double_key(self):
        done = {'S1': {'name': '剧', 'ts': 1}}
        assert facts.manual_done_for(done, ['S1'], '8888') is True    # Emby id 命中
        assert facts.manual_done_for(done, ['S9'], '8888') is False   # 两键都不在 → 未标记
        assert facts.manual_done_for({'8888': {'ts': 1}}, ['S9'], '8888') is True  # tmdb_id 键也认
        assert facts.manual_done_for({}, ['S1'], '8888') is False

    def test_get_series_status_uses_done_set(self, isolated_db, monkeypatch):
        def fake(path, params=None, method='GET', timeout=15):
            params = params or {}
            if params.get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'S1', 'Name': '剧', 'Path': '/media/local/x',
                                   'ProviderIds': {'Tmdb': '8888'}}]}
            return {'Items': []}
        monkeypatch.setattr(emby, 'emby_request', fake)
        monkeypatch.setattr(facts, '_load_tmdb_info', lambda tid: {
            'name': '剧', 'status': 'Ended', 'aired': {(1, e) for e in range(1, 26)},
            'total_episodes': 25, 'declared_total': 25,
            'seasons': {1: {'episode_count': 25}}})
        # 只有部分集在库 + 已完结 → 本应缺集；Emby id 键的完结标记 → 完整
        monkeypatch.setattr(facts, 'series_episodes', lambda rows: {
            'episodes': {facts.ep_key(1, e) for e in range(1, 23)},
            'local': set(), 'share': set(), 'special': set(), 'created': {},
            'series_ids': ['S1'], 'series_name': '剧'})
        out = facts.get_series_status('8888', manual_done_set={'S1': {'ts': 1}})
        assert out['classification']['status'] == 'complete'
        assert out['classification']['manual_done'] is True
