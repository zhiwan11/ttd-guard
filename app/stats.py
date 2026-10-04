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
from . import state, lib, storage, sync

log = logging.getLogger('media_agent')


def _recompute_all_stats():
    """单次遍历双库，同时更新「分类统计」与「STRM 计数」两套缓存。
    之前两处各自独立遍历，时间不同/网络挂载抖动会导致数字对不上；
    统一从同一次遍历取数，保证总览卡片与总库分类统计永远一致。"""
    CATS = [
        ('\U0001F476 儿童节目', ['儿童']),
        ('\U0001F3A4 演唱会',       ['演唱会']),
        ('\U0001F3AA 综艺剧',       ['综艺']),
        ('\u26e9\ufe0f 动漫剧',     ['动漫']),
        ('\U0001F9F8 动画电影',   ['动画电影']),
        ('\U0001F4FD\ufe0f 纪录片',  ['纪录']),
        ('\U0001F1E8\U0001F1F3 国产剧', ['国产剧集', '国产剧']),
        ('\U0001F1FA\U0001F1F8 欧美剧', ['欧美剧集', '欧美剧', '其他剧集']),
        ('\U0001F1EF\U0001F1F5 日韩剧', ['日韩剧集', '日韩剧']),
        ('\U0001F1E8\U0001F1F3 华语电影', ['国产电影', '华语电影']),
        ('\U0001F310 外语电影', ['欧美电影', '日韩电影', '外语电影', '其他电影']),
    ]
    cat_names = [c[0] for c in CATS]

    out = {
        'local': {c: 0 for c in cat_names},
        'share': {c: 0 for c in cat_names},
        'local_other': 0,
        'share_other': 0,
        'local_total': 0,
        'share_total': 0,
    }

    for root, key in ((state.L_ROOT, 'local'), (state.S_ROOT, 'share')):
        if not root.exists():
            continue
        for f in root.rglob('*.strm'):
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                continue
            matched = None
            for part in parts:
                for cat_name, kws in CATS:
                    hit = False
                    for kw in kws:
                        if kw in part:
                            hit = True
                            break
                    if hit:
                        matched = cat_name
                        break
                if matched:
                    break
            if matched:
                out[key][matched] += 1
            else:
                out[key + '_other'] += 1
            out[key + '_total'] += 1

    rows = []
    for cat in cat_names:
        rows.append({
            'name': cat,
            'local': out['local'][cat],
            'share': out['share'][cat],
            'total': out['local'][cat] + out['share'][cat],
        })
    result = {
        'status': 'success',
        'rows': rows,
        'local_total': out['local_total'],
        'share_total': out['share_total'],
        'local_other': out['local_other'],
        'share_other': out['share_other'],
    }
    now = time.time()
    state._lib_stats_cache['ts'] = now
    state._lib_stats_cache['data'] = result
    return result

def action_library_stats(args):
    # 缓存命中则直接返回
    now = time.time()
    if state._lib_stats_cache['data'] is not None and (now - state._lib_stats_cache['ts']) < state._CACHE_TTL:
        return state._lib_stats_cache['data']
    return _recompute_all_stats()

def _get_strm_counts():
    """STRM 总数（S7 收敛）：单一来源 = 分类统计的 5 分钟内存缓存（同一次遍历产出）。
    旧的「strm_count 内存+磁盘第二套缓存」已删——两套缓存各自计时是总览数字
    与分类统计对不上的根源；现在只有一个缓存层，数字天然一致。
    缓存过期且后台无人在算时同步重算（rglob 大库慢，但每 5 分钟最多一次）。"""
    now = time.time()
    data = state._lib_stats_cache['data']
    if data is not None and (now - state._lib_stats_cache['ts']) < state._CACHE_TTL:
        return int(data.get('local_total', 0)), int(data.get('share_total', 0))
    result = _recompute_all_stats()
    return int(result['local_total']), int(result['share_total'])

def _clear_stats_caches():
    """只清统计内存缓存（供失效总线回调调用；不再触发 bump，避免递归）。"""
    state._lib_stats_cache['ts'] = 0
    state._lib_stats_cache['data'] = None

def invalidate_stats_cache():
    """清空统计缓存（删除后调用）。统计属于 library 域，统一走失效总线：
    版本号 +1 并执行注册的清理回调（其它进程/浏览器据此自动刷新）。"""
    sync.bump('library')

def invalidate_media_caches(keep_emby_lib=False):
    """文件变动后统一失效内存缓存（分集 / Emby 索引 / 片库映射 / lib.Lib 快照 / 统计）。

    经失效总线 bump('library')：版本号 +1、执行注册回调、给磁盘(库)缓存打 stale 标记。
    keep_emby_lib=True：单剧删除流程随后会就地修补片库映射缓存，这里先把内存副本存下来，
    bump 的回调清完再放回去，避免整页重跑 TMDB 对照（磁盘副本由删除流程自行修补）。"""
    if keep_emby_lib:
        saved_ts, saved_data = state._emby_lib_cache['ts'], state._emby_lib_cache['data']
        sync.bump('library')
        state._emby_lib_cache['ts'], state._emby_lib_cache['data'] = saved_ts, saved_data
        state._stale.discard('emby_overview')   # 该缓存已就地修补，不算 stale
        return
    sync.bump('library')
