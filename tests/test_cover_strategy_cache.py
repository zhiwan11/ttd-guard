# -*- coding: utf-8 -*-
"""get_cover_strategy 按配置签名缓存 + recognize_dims lru_cache 的回归测试。

重点保证缓存的三条语义：
  - 签名未变时命中缓存（首次/二次调用返回等价且同一对象）；
  - 直接改写配置文件（mtime/size 签名变化）或经 save_config 落盘后立即生效；
  - 配置文件不存在（sig=None）时回落默认规则集，且默认值同样被缓存。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_cover_cache_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import config  # noqa: E402
import core  # noqa: E402


def _isolate(monkeypatch, tmp_path):
    """把配置文件指到本测试专属目录，并清空两层缓存，保证互不串扰。"""
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    config._CFG_CACHE['data'] = None
    config._COVER_STRATEGY_CACHE['data'] = None


def _save_cover(monkeypatch, tmp_path, rules):
    _isolate(monkeypatch, tmp_path)
    cfg = config.load_config()
    cfg['strategy_cover'] = json.dumps({'rules': rules}, ensure_ascii=False)
    config.save_config(cfg)


class TestCoverStrategyCache:
    def test_second_call_hits_cache(self, monkeypatch, tmp_path):
        _save_cover(monkeypatch, tmp_path, [{'key': 'resolution', 'enabled': True}])
        first = config.get_cover_strategy()
        assert [r['key'] for r in first['rules']][0] == 'resolution'
        second = config.get_cover_strategy()
        assert second is first  # 签名未变 → 命中缓存，返回同一对象
        assert second == first

    def test_direct_file_rewrite_invalidates_by_signature(self, monkeypatch, tmp_path):
        _save_cover(monkeypatch, tmp_path, [{'key': 'resolution', 'enabled': True}])
        first = config.get_cover_strategy()
        assert [r['key'] for r in first['rules']][0] == 'resolution'
        # 绕过 save_config 直接改写配置文件：签名（mtime/size）变化必须立即生效
        data = json.loads(config.CONFIG_FILE.read_text(encoding='utf-8'))
        data['strategy_cover'] = json.dumps({'rules': [{'key': 'source', 'enabled': False}]},
                                            ensure_ascii=False)
        config.CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        second = config.get_cover_strategy()
        assert second is not first
        assert [r['key'] for r in second['rules']][0] == 'source'
        assert second['rules'][0]['enabled'] is False

    def test_update_cover_strategy_takes_effect_immediately(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        default = config.get_cover_strategy()
        assert default['rules'][0]['key'] == 'release_group'  # 默认规则顺序的兜底确认
        out = config.update_cover_strategy({'rules': [{'key': 'audio', 'enabled': True}]})
        assert [r['key'] for r in out['rules']][0] == 'audio'
        # Web 保存路径经 save_config 落盘，缓存已同步失效
        now = config.get_cover_strategy()
        assert now == out
        on_disk = json.loads(config.CONFIG_FILE.read_text(encoding='utf-8'))
        assert json.loads(on_disk['strategy_cover'])['rules'][0]['key'] == 'audio'

    def test_missing_config_file_falls_back_to_default(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)  # tmp_path 里没有 config.json → sig=None
        s = config.get_cover_strategy()
        assert s == core.cover_default_strategy()
        assert len(s['rules']) == 7
        # sig=None 的默认策略同样被缓存（data 不为 None 即视为有缓存）
        assert config.get_cover_strategy() is s


class TestRecognizeDimsCache:
    def test_same_name_returns_same_object(self):
        name = 'The.Matrix.1999.2160p.UHD.BluRay.DV.P7.DDP5.1.Atmos.10bit-ADWeb.mkv'
        d1 = core.recognize_dims(name)
        d2 = core.recognize_dims(name)
        assert d1 is d2  # lru_cache 共享缓存对象
        assert d1 == d2
        other = core.recognize_dims('a.1080p.WEB-DL.AAC.mkv')
        assert other is not d1
        assert other['resolution'] == '1080p'

    def test_compare_cover_behavior_unchanged(self):
        # 对照 tests/test_core.py 既有断言，加缓存后行为不变
        c = core.cover_default_strategy()
        assert core.compare_cover('a.1080p.BluRay.mkv', 'a.2160p.WEB-DL.mkv', c) == 1
        assert core.compare_cover('a.DV.P7.mkv', 'a.DV.P8.mkv', c) == 1
        assert core.compare_cover('a.mkv', 'b.mkv', c) == 0
        assert core.compare_cover('a.2160p.WEB-DL.mkv', 'a.1080p.BluRay.mkv', c) == -1
