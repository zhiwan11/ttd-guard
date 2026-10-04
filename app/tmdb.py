# -*- coding: utf-8 -*-
"""从 engine.py 拆出。共享状态在 state，跨模块符号按所属模块 `模块.名字` 在调用时访问（monkeypatch 所属模块即可穿透）。"""
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

from . import config as _cfg
from . import logger
from .core import (esc, parse_season_dir, get_ep, title_key, governance_title_key,
                   analyze_season_episodes, parse_emby_library, quality_label, RE_SXXEXX)
from . import state, emby, lib, storage, media, sync, facts

log = logging.getLogger('media_agent')


TMDB_BASE = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
TMDB_LANG = os.environ.get('TMDB_LANG', 'zh-CN')
TMDB_IMG  = os.environ.get('TMDB_IMG', 'https://image.tmdb.org/t/p/w500')
TMDB_INFO_TTL = 24 * 3600
REGION_MAP = {
    'cn': {'with_origin_country': 'CN'}, 'hk': {'with_origin_country': 'HK'},
    'tw': {'with_origin_country': 'TW'}, 'jp': {'with_origin_country': 'JP'},
    'kr': {'with_origin_country': 'KR'}, 'us': {'with_origin_country': 'US'},
}
SORT_MAP = {
    'popularity': 'popularity.desc', 'release': 'primary_release_date.desc',
    'rating': 'vote_average.desc',
}
GENRE_MAP = {
    # 与 TMDB 官方 genre 列表对齐（movie: /genre/movie/list, tv: /genre/tv/list）
    '动作':   {'movie': 28,    'tv': 10759},
    '冒险':   {'movie': 12,    'tv': None},
    '喜剧':   {'movie': 35,    'tv': 35},
    '犯罪':   {'movie': 80,    'tv': 80},
    '纪录':   {'movie': 99,    'tv': 99},
    '剧情':   {'movie': 18,    'tv': 18},
    '家庭':   {'movie': 10751, 'tv': 10751},
    '奇幻':   {'movie': 14,    'tv': None},
    '历史':   {'movie': 36,    'tv': None},
    '恐怖':   {'movie': 27,    'tv': None},
    '音乐':   {'movie': 10402, 'tv': None},
    '悬疑':   {'movie': 9648,  'tv': 9648},
    '爱情':   {'movie': 10749, 'tv': None},
    '科幻':   {'movie': 878,   'tv': 10765},
    '惊悚':   {'movie': 53,    'tv': None},
    '战争':   {'movie': 10752, 'tv': 10768},
    '西部':   {'movie': 37,    'tv': 37},
    '动画':   {'movie': 16,    'tv': 16},
    '儿童':   {'movie': None,  'tv': 10762},
    '新闻':   {'movie': None,  'tv': 10763},
    '真人秀': {'movie': None,  'tv': 10764},
    '肥皂剧': {'movie': None,  'tv': 10766},
    '脱口秀': {'movie': None,  'tv': 10767},
    '电视电影': {'movie': 10770, 'tv': None},
}
_PRUNE_INTERVAL = 3600          # tmdb_cache 表过期清理：每小时最多一次
_last_prune = {'ts': 0.0}


class TmdbError(Exception):
    pass

class Tmdb:
    """TMDB 客户端。响应缓存两级：实例内存（本轮扫描）+ SQLite tmdb_cache 表（每个请求一行，
    请求成功立即落库——多个 Tmdb 实例并发也不会互相覆盖）。"""

    def __init__(self):
        self.key = (state.RUNTIME_CFG.get('tmdb_key') or '').strip()
        self.calls = self.hits = 0
        self.cache = {}

    def get(self, path, ttl=6 * 3600, **params):
        params.setdefault('language', TMDB_LANG)
        ck = path + '?' + urllib.parse.urlencode(sorted(params.items()))
        hit = self.cache.get(ck)
        if hit and time.time() - hit['ts'] < ttl:
            self.hits += 1
            return hit['data']
        cached = storage.db_tmdb_get(ck, ttl)
        if cached is not None:      # 库内命中不回填内存：内存里的 ts 会比库里新，绕过调用方更短的 ttl
            self.hits += 1
            return cached
        headers = {}
        if self.key.startswith('eyJ'):
            headers['Authorization'] = f'Bearer {self.key}'
        else:
            params['api_key'] = self.key
        url = f'{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}'
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
                    data = json.loads(r.read().decode('utf-8'))
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403): raise TmdbError(f'TMDB Key 无效或无权限 (HTTP {e.code})')
                if e.code == 429:
                    time.sleep(int(e.headers.get('Retry-After', '2')) + 1); continue
                if e.code == 404: return None
                if attempt == 2: raise TmdbError(f'TMDB 返回 HTTP {e.code}')
                time.sleep(1.5)
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt == 2: raise TmdbError(f'无法访问 TMDB: {e}')
                time.sleep(1.5)
        if data is None: raise TmdbError('TMDB 多次请求失败')
        self.cache[ck] = {'ts': time.time(), 'data': data}
        storage.db_tmdb_put(ck, data)
        self.calls += 1
        time.sleep(0.03)
        return data

    def save(self):
        """兼容旧调用点：数据已在 get() 时逐条落库，这里只做过期清理（每小时最多一次，保留 24 小时）。"""
        now = time.time()
        if now - _last_prune['ts'] < _PRUNE_INTERVAL:
            return
        _last_prune['ts'] = now
        storage.db_tmdb_prune(storage.TMDB_MAX_AGE)


def action_explore(args):
    """S2 第一段（§5.2）：探索目录只读 TMDB，立即返回卡片，不等 Emby。

    一次取 page*2-1 与 page*2 两页拼 40 张卡；卡片带 in_emby/eps 占位字段
    （第二段 /api/explore/status 才现查 Emby 就地更新）。TMDB 失败直接报错，
    不降级成旧缓存（§5.2 既定口径：TMDB 网络问题不在考虑范围）。
    """
    region = getattr(args, 'region', 'all') or 'all'
    year = (getattr(args, 'year', '') or '').strip()
    sort = getattr(args, 'sort', 'popularity') or 'popularity'
    media = getattr(args, 'media', 'movie') or 'movie'
    page = int(getattr(args, 'page', 1) or 1)
    query = (getattr(args, 'q', '') or '').strip()
    genre = (getattr(args, 'genre', '') or '').strip()

    t = Tmdb()
    if not t.key:
        return {'status': 'error', 'message': '未配置 TMDB_KEY'}

    if query:
        # 搜索：media=all 走 /search/multi 一次搜到电影+剧集（结果带 media_type，可后切），
        # 明确选了 movie/tv 时仍用精确的分类型搜索。
        if media == 'all':
            tmdb_path = '/search/multi'
        else:
            tmdb_path = '/search/tv' if media == 'tv' else '/search/movie'
        params = {'query': query, 'page': page, 'include_adult': 'false'}
    else:
        # 浏览：TMDB discover 没有 all，媒体类型回落到 movie。
        if media == 'all':
            media = 'movie'
        tmdb_path = '/discover/tv' if media == 'tv' else '/discover/movie'
        params = {'page': page, 'sort_by': SORT_MAP.get(sort, 'popularity.desc'), 'vote_count.gte': 5}
        if region != 'all' and region in REGION_MAP: params.update(REGION_MAP[region])
        if year:
            params['first_air_date_year' if media == 'tv' else 'primary_release_year'] = year
        if genre and genre in GENRE_MAP:
            gid = GENRE_MAP[genre].get(media)
            if gid:
                params['with_genres'] = str(gid)

    # 一次请求 TMDB 2 页，合并为 40 张卡片
    try:
        params1 = dict(params); params1['page'] = page * 2 - 1
        params2 = dict(params); params2['page'] = page * 2
        res1 = t.get(tmdb_path, **params1) or {}
        res2 = t.get(tmdb_path, **params2) or {}
    except TmdbError as e:
        # TMDB 失败直接报错，不降级成旧缓存（§5.2）
        return {'status': 'error', 'message': str(e)}
    results = (res1.get('results') or []) + (res2.get('results') or [])
    total_pages_raw = res1.get('total_pages') or 1

    cards = []
    for item in results:
        # multi 搜索时每张卡的类型来自 media_type（'movie'/'tv'/'person'），discover 用全局 media
        item_media = item.get('media_type') or media
        if item_media == 'person':
            continue  # /search/multi 会返回人物，探索页不展示
        if item_media not in ('movie', 'tv'):
            item_media = media
        tmdb_id = str(item.get('id', ''))
        title = item.get('title') or item.get('name') or ''
        date_str = item.get('release_date') or item.get('first_air_date') or ''
        year_str = date_str[:4] if date_str else ''
        rating = item.get('vote_average') or 0
        poster = item.get('poster_path')
        poster_url = f'{TMDB_IMG}{poster}' if poster else ''
        # 占位字段：第二段 /api/explore/status 才现查 Emby（§5.2 两段式）
        cards.append({
            'tmdb_id': tmdb_id, 'title': title, 'year': year_str,
            'rating': round(rating, 1) if rating else None, 'poster': poster_url,
            'in_emby': None, 'in_local': False, 'in_share': False,
            'emby_id': None,
            'type': item_media,
            'eps': None, 'eps_source': None,
        })
    cards = cards[:40]

    t.save()
    return {'status': 'success', 'page': page,
            'total_pages': min(max(1, total_pages_raw // 2), 20),
            'total_results': res1.get('total_results', 0),
            'cards': cards, 'is_search': bool(query),
            'tmdb_calls': t.calls, 'tmdb_hits': t.hits}


def normalize_tmdb_info(tmdb_info):
    """把 TMDB 数据规整成 facts.classify 的契约形态（aired 为 (季,集) 键集合）。

    两种输入都接受（§2.4/§5.3 已播唯一口径）：
      - 原始 /tv/{id} 响应 / _tmdb_series_info 产物：走 media.tmdb_aired_map 的
        **截断口径**（按 last_episode_to_air 裁剪、排除 S00）——未播集绝不进 aired；
      - 旧 fake 形态（晨报 _resync 由缓存逐季集数拼出）没有 last_episode_to_air，
        截断无依据，才退回按各季 episode_count 全量展开。
    已含 aired 键的（facts 形态）原样返回；None 返回 None。
    """
    if not tmdb_info or 'aired' in tmdb_info:
        return tmdb_info
    aired_map = media.tmdb_aired_map(tmdb_info)      # 有 last_episode_to_air → 精确截断
    if aired_map:
        aired = set()
        for sn, n in aired_map.items():
            for e in range(1, n + 1):
                k = facts.ep_key(sn, e)
                if k:
                    aired.add(k)
    else:
        # 无 last_episode_to_air 的回退口径（与旧实现 season_count 求和等价）
        aired = set()
        for s in (tmdb_info.get('seasons') or []):
            sn = s.get('season_number')
            if sn is None or int(sn) <= 0:
                continue
            for e in range(1, int(s.get('episode_count') or 0) + 1):
                k = facts.ep_key(int(sn), e)
                if k:
                    aired.add(k)
    fake = dict(tmdb_info)
    fake['aired'] = aired
    totals = media.tmdb_totals(tmdb_info)
    fake['total_episodes'] = totals['aired'] if aired_map else len(aired)
    fake['declared_total'] = totals['declared']
    return fake


def classify_series_by_tmdb(local_seasons, tmdb_info, manual_done=False):
    """按 TMDB 已播集数对照片库（S1 起为 facts.classify 的薄封装，§5.3 唯一口径）。

    输入输出结构与既有调用方（片库映射 / 晨报 / TMDB 对照扫描）完全兼容：
    ``match_status`` 由 facts.classify 给出**中文六态**（完整/缺集/超集/在更/
    在更·落后/未匹配；D3 决策：在更·落后单独一档），同时保留 ``status_cn``/
    ``missing_ranges``（精确集号区间）/ ``extra_total`` 扩展字段。
    缺集判定从「数集数相减」改为 facts.classify 的**集合差集**——不再出现
    「缺一个、多一个互相抵消」的假对齐。
    """
    from . import facts
    keys = set()
    for s in (local_seasons or []):
        if s.get('season', 0) > 0:
            for e in range(1, int(s.get('episodes') or 0) + 1):
                k = facts.ep_key(s['season'], e)
                if k:
                    keys.add(k)
    tmdb_info = normalize_tmdb_info(tmdb_info)
    cls = facts.classify(keys, tmdb_info, manual_done=manual_done)
    local_map = {s['season']: s['episodes'] for s in (local_seasons or []) if s['season'] > 0}
    local_total = sum(local_map.values())
    season_diff = []
    per_season_aired = _aired_per_season(tmdb_info)
    for sn in sorted(set(local_map.keys()) | set(per_season_aired.keys())):
        l = int(local_map.get(sn, 0) or 0); t_ = int(per_season_aired.get(sn, 0) or 0)
        if l == t_: st = 'aligned'
        elif l < t_: st = 'missing'
        else: st = 'extra'
        season_diff.append({'season': sn, 'local': l, 'tmdb': t_, 'diff': l - t_, 'status': st})
    return {'match_status': cls['status'], 'status_cn': cls['label'],
            'missing_ranges': cls['missing_ranges'], 'extra_total': len(cls['extra']),
            'manual_done': bool(cls.get('manual_done')),
            'tmdb_status': cls.get('tmdb_status') or '',
            'local_total': local_total,
            'tmdb_total': (tmdb_info or {}).get('total_episodes') or 0,
            'declared_total': (tmdb_info or {}).get('declared_total') or 0,
            'diff': local_total - ((tmdb_info or {}).get('total_episodes') or 0),
            'seasons': season_diff}


def _aired_per_season(tmdb_info):
    """已播集按季计数（用于逐季明细行）：优先真 TMDB 响应的 aired 集合；
    fake 形态直接数 seasons[].episode_count。"""
    from . import facts as _facts
    if not tmdb_info:
        return {}
    if 'aired' in tmdb_info:
        counts = {}
        for k in _facts._as_keys(tmdb_info.get('aired')):
            pair = _facts.parse_ep_key(k)
            if pair:
                counts[pair[0]] = counts.get(pair[0], 0) + 1
        return counts
    return {int(s['season_number']): int(s.get('episode_count') or 0)
            for s in (tmdb_info.get('seasons') or [])
            if s.get('season_number') is not None and int(s.get('season_number') or 0) > 0}

def _emby_series_live_eps(series_tmdb_id, fallback_id=None, use_cache=True):
    """实时查某剧在 Emby 的分集集合（薄包装 media.series_live，不读任何快照）。

    返回 {'episodes': set[(季,集)], 'local_eps': int, 'share_eps': int,
          'have_eps': int, 'empty': bool}；完全查不到时返回 None。
    ``empty=True`` 表示「查到了 Series 但可归属到两个库根的分集为 0」——调用方应保留
    旧值而不是用 0 覆盖（避免 Emby 短暂异常时把卡片打成「未入库」）。
    """
    live = media.series_live(series_tmdb_id, fallback_id, use_cache=use_cache)
    if not live:
        return None
    local = live.get('local') or set()
    share = live.get('share') or set()
    have_n = len(local | share)
    return {'episodes': live.get('episodes') or set(), 'local_eps': len(local), 'share_eps': len(share),
            'have_eps': have_n, 'empty': have_n == 0}

def _tmdb_aired_set_from_info(info):
    """从已取得的 TMDB /tv/{id} 原始响应计算已播 (季,集)，不重复请求 TMDB。"""
    aired = set()
    for sn, n in media.tmdb_aired_map(info).items():
        aired.update((sn, e) for e in range(1, n + 1))
    return aired

def _tmdb_series_info(tmdb_id):
    """查 TMDB 已播集数、状态、季结构。
    已播集以 last_episode_to_air 为界：之前的季整季计入，最后播出的季只计到该集；
    连载季 episode_count 含未播集，直接求和会让在更的剧永远「缺集」。
    缺 last_episode_to_air 时退回旧口径（各季 episode_count 全部计入）。"""
    try:
        t = Tmdb()
        info = t.get(f'/tv/{tmdb_id}', ttl=TMDB_INFO_TTL)
        t.save()
        if not info: return None
        aired_seasons = {}
        for s in (info.get('seasons') or []):
            sn = s.get('season_number')
            if sn is None or sn <= 0: continue
            aired_seasons[sn] = {
                'episode_count': s.get('episode_count', 0) or 0,
                'air_date': s.get('air_date') or '',
            }
        last = info.get('last_episode_to_air') or {}
        aired = _tmdb_aired_set_from_info(info)
        totals = media.tmdb_totals(info)
        return {
            'name': info.get('name'),
            'status': info.get('status', ''),
            'last_episode_to_air': last,
            'seasons': aired_seasons,
            'total_episodes': totals['aired'],      # 已播集数（不含未播集 / S00）
            'declared_total': totals['declared'],   # 标称总集数（展示用）
            'aired': aired,                         # {(季, 集)}
        }
    except Exception as e:
        log.warning('TMDB 订阅查询失败 %s: %s', tmdb_id, e)
        return None
