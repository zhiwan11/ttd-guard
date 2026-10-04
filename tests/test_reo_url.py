# -*- coding: utf-8 -*-
"""S6 reo 链接生成回归（白皮书 §5.6，验收 A9）：

- tv/movie 模板不串类型：同一 tmdb_id 在两端各归各（A9 口径：TMDB 电影/剧集
  ID 空间独立，同一数字是两部不同作品）；
- 占位符替换：{tmdb_id}/{title}/{year}；{title} URL 编码；
- 缺 TMDB ID → 落 reo_search_url 兜底（{title}/{year}）；搜索模板也空/标题空 → ''；
- 空模板时 build_url 返回 ''、reo_enabled() False——接口不产生 reo 链接；
- media_type 非法值拒绝。
"""
import os

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import config as _cfg, reo


@pytest.fixture()
def reo_cfg(monkeypatch):
    """标准模板配置（每个用例独立，测试后还原）。"""
    monkeypatch.setattr(_cfg, 'load_config', lambda: {
        'reo_tv_url': 'https://reo.example/tv/{tmdb_id}',
        'reo_movie_url': 'https://reo.example/movie/{tmdb_id}',
        'reo_search_url': 'https://reo.example/search?q={title}({year})',
    })
    yield


@pytest.fixture()
def empty_cfg(monkeypatch):
    monkeypatch.setattr(_cfg, 'load_config', lambda: {
        'reo_tv_url': '', 'reo_movie_url': '', 'reo_search_url': ''})
    yield


class TestTypeSeparation:
    """A9 核心：同一数字在 tv/movie 两端各归各，绝不串到另一类型。"""

    def test_same_id_different_templates(self, reo_cfg):
        tv = reo.build_url('tv', tmdb_id='8888')
        mv = reo.build_url('movie', tmdb_id='8888')
        assert tv == 'https://reo.example/tv/8888'
        assert mv == 'https://reo.example/movie/8888'
        assert tv != mv and '/tv/' in tv and '/movie/' in mv

    def test_missing_movie_template_never_falls_back_to_tv(self, monkeypatch):
        """电影模板空 → 不借剧集模板（宁可空/兜底搜索，不能串类型）。"""
        monkeypatch.setattr(_cfg, 'load_config', lambda: {
            'reo_tv_url': 'https://reo.example/tv/{tmdb_id}',
            'reo_movie_url': '',
            'reo_search_url': ''})
        assert reo.build_url('movie', tmdb_id='8888') == ''
        assert reo.build_url('tv', tmdb_id='8888') == 'https://reo.example/tv/8888'


class TestPlaceholders:

    def test_placeholder_replacement(self, monkeypatch):
        monkeypatch.setattr(_cfg, 'load_config', lambda: {
            'reo_tv_url': 'https://reo.example/tv/{tmdb_id}',
            'reo_movie_url': '',
            'reo_search_url': 'https://reo.example/s?q={title}+{year}'})
        assert reo.build_url('tv', tmdb_id='42', title='测试剧', year='2026') \
            == 'https://reo.example/tv/42'
        # {title} URL 编码
        assert '%E6%B5%8B%E8%AF%95%E5%89%A7' in reo.build_url('tv', title='测试剧')

    def test_title_encoded_in_search(self, reo_cfg):
        url = reo.build_url('tv', title='复仇者 联盟', year='2019', force_search=True)
        assert url.startswith('https://reo.example/search?q=')
        assert '%E5%A4%8D%E4%BB%87%E8%80%85' in url          # 「复仇者」UTF-8 编码
        assert '%20' in url                                   # safe='' 空格也编码
        assert '2019' in url


class TestFallback:

    def test_no_tmdb_id_falls_back_to_search(self, reo_cfg):
        url = reo.build_url('tv', tmdb_id='', title='测试剧', year='2026')
        assert url == 'https://reo.example/search?q=%E6%B5%8B%E8%AF%95%E5%89%A7(2026)'

    def test_force_search_prefers_search_template(self, reo_cfg):
        url = reo.build_url('tv', tmdb_id='8888', title='测试剧', year='2026',
                            force_search=True)
        assert 'search' in url and '8888' not in url

    def test_no_search_template_no_title_returns_empty(self, reo_cfg, monkeypatch):
        """搜索模板在但标题空 → ''；无搜索模板 → ''。"""
        assert reo.build_url('tv', tmdb_id='', title='') == ''
        monkeypatch.setattr(_cfg, 'load_config', lambda: {
            'reo_tv_url': 'https://reo.example/tv/',
            'reo_movie_url': '', 'reo_search_url': ''})
        assert reo.build_url('tv', tmdb_id='', title='测试剧') == ''

    def test_empty_title_with_search_template_returns_empty(self, reo_cfg):
        assert reo.build_url('tv', tmdb_id='', title='') == ''

    def test_invalid_media_type_rejected(self, reo_cfg):
        """media_type 不是 tv/movie → ''（类型即路由，不猜）。"""
        assert reo.build_url('book', tmdb_id='8888') == ''
        assert reo.build_url('', tmdb_id='8888') == ''


class TestEmptyConfig:

    def test_all_empty_returns_empty_and_disabled(self, empty_cfg):
        assert reo.build_url('tv', tmdb_id='8888') == ''
        assert reo.build_url('movie', tmdb_id='8888') == ''
        assert reo.build_url('tv', title='测试剧') == ''
        assert reo.reo_enabled() is False

    def test_partial_config_enables(self, monkeypatch):
        monkeypatch.setattr(_cfg, 'load_config', lambda: {
            'reo_tv_url': '', 'reo_movie_url': 'https://reo.example/movie/{tmdb_id}',
            'reo_search_url': ''})
        assert reo.reo_enabled() is True
        assert reo.build_url('tv', tmdb_id='8888') == ''    # 剧集模板空仍返回 ''
        assert reo.build_url('movie', tmdb_id='8888') == 'https://reo.example/movie/8888'


class TestApiWiring:

    AUTH = ('admin', 'test-pass-123')

    def test_config_keys_editable_and_default_empty(self, isolated_db):
        """/api/config 能读写三个键；默认值为空字符串（D6：绝不猜格式）。"""
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        r = c.get('/api/config', auth=self.AUTH)
        cfg = r.json()['config']
        assert cfg.get('reo_tv_url') == '' and cfg.get('reo_movie_url') == ''
        assert cfg.get('reo_search_url') == ''
        r2 = c.post('/api/config', json={
            'reo_tv_url': 'https://reo.example/tv/{tmdb_id}',
            'reo_movie_url': 'https://reo.example/movie/',
            'reo_search_url': 'https://reo.example/s?q={title}'}, auth=self.AUTH)
        assert r2.json()['status'] == 'success'
        saved = c.get('/api/config', auth=self.AUTH).json()['config']
        assert saved['reo_tv_url'] == 'https://reo.example/tv/{tmdb_id}'
        assert saved['reo_movie_url'] == 'https://reo.example/movie/'
        assert saved['reo_search_url'] == 'https://reo.example/s?q={title}'

    def test_dashboard_reports_reo_enabled(self, isolated_db, monkeypatch):
        """/api/dashboard 带 reo.enabled（前端渲染按钮的双保险之一）。
        reo_enabled 打桩隔离：config.json 跟 AGENT_DATA 走（不随 STATE_DIR 隔离），
        前面用例 POST 过模板会真实落盘。"""
        monkeypatch.setattr(reo, 'reo_enabled', lambda: False)
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        r = c.get('/api/dashboard', auth=self.AUTH)
        assert r.status_code == 200
        assert r.json().get('reo') == {'enabled': False}    # 默认全空 → False


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    yield sd
    storage.close_all()


from app import state, storage  # noqa: E402
