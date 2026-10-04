# -*- coding: utf-8 -*-
"""片库：入库 / 统计 / Emby / 搜索 / 日志 / 删除 / 探索（v1.7.7 自 main.py 拆出）"""
import logging
import time, json, threading, urllib.parse, urllib.request
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app import config as _cfg
from app import state, governance, wash, morning, tmdb, ingest, emby, stats, logger, facts, storage
from app.routers.deps import auth, Args

router = APIRouter()


@router.get('/api/ingest', dependencies=[Depends(auth)])
def api_ingest(full: int = 0, force: int = 0):
    """
    full=1 完整清单，force=1 强制立即刷新
    默认读缓存（10 分钟时效），过期自动刷新
    """
    try:
        kw = ('full ' if full else '') + ('force ' if force else '')
        return ingest.action_stats(Args(kw=kw.strip()))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/status', dependencies=[Depends(auth)])
def api_ingest_status():
    """只读缓存状态，不触发刷新"""
    data = ingest.read_ingest_cache()
    if not data:
        return {'status': 'success', 'has_cache': False}
    return {'status': 'success', 'has_cache': True,
            'ts': data.get('ts', 0),
            'age_sec': int(time.time() - data.get('ts', 0)),
            'ok': data.get('ok', True),
            'warning': data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', '')),
            'stats': data.get('stats', {})}


@router.get('/api/stats', dependencies=[Depends(auth)])
def api_stats(full: int = 0, force: int = 0):
    kw = ('full ' if full else '') + ('force ' if force else '')
    return ingest.action_stats(Args(kw=kw.strip()))


@router.get('/api/played', dependencies=[Depends(auth)])
def api_played():
    return ingest.action_played(Args())


@router.get('/api/search', dependencies=[Depends(auth)])
def api_search(q: str):
    if not q.strip(): raise HTTPException(400, '关键词不能为空')
    return ingest.action_search(Args(kw=q))


@router.get('/api/records', dependencies=[Depends(auth)])
def api_records(n: int = 50):
    try:
        snap = morning.daily_consistency_snapshot(False)
        return {'status': 'success', 'records': logger.read_recent(limit=n),
                'consistency': {'rule_sig': snap.get('rule_sig'),
                                'library_ts': (snap.get('library') or {}).get('ts', 0),
                                'ingest_ts': (snap.get('ingest') or {}).get('ts', 0),
                                'governance_ts': (snap.get('governance') or {}).get('ts', 0)}}
    except Exception:
        return {'status': 'success', 'records': logger.read_recent(limit=n), 'consistency': {}}


@router.get('/api/logs', dependencies=[Depends(auth)])
def api_logs(n: int = 35):
    return {'status': 'success', 'text': logger.to_text(n)}


@router.get('/api/emby/poster/{item_id}')
def api_emby_poster(item_id: str):
    if not state.EMBY_KEY: raise HTTPException(500, '未配置 EMBY_KEY')
    url = f'{state.EMBY_HOST}/Items/{urllib.parse.quote(item_id, safe="")}/Images/Primary'
    req = urllib.request.Request(url, headers={'X-Emby-Token': state.EMBY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(); ct = r.headers.get('Content-Type', 'image/jpeg')
        return Response(content=data, media_type=ct)
    except Exception as e:
        raise HTTPException(404, str(e))


@router.get('/api/explore', dependencies=[Depends(auth)])
def api_explore(region: str = 'all', year: str = '', sort: str = 'popularity',
                media: str = 'movie', page: int = 1, q: str = '', genre: str = ''):
    try:
        return tmdb.action_explore(Args(region=region, year=year,
                                              sort=sort, media=media,
                                              page=page, q=q, genre=genre))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _tmdb_scan_progress_dict():
    """后台 TMDB 对照的进度快照（并入 api_emby_library，原 get_tmdb_scan_progress 收口）。"""
    p = dict(morning.state._tmdb_scan_progress)
    if p.get('running') and p.get('started_at'):
        p['elapsed_sec'] = int(time.time() - p['started_at'])
        p['eta_sec'] = int(p['elapsed_sec'] / p['done'] * (p['total'] - p['done'])) if p.get('done') else None
        p['percent'] = int(p['done'] / p['total'] * 100) if p.get('total') else 0
    else:
        p['elapsed_sec'] = (p.get('finished_at') - p.get('started_at')) if p.get('finished_at') and p.get('started_at') else 0
        p['eta_sec'] = None
        p['percent'] = 100 if p.get('finished_at') else 0
    return p


EXPLORE_STATUS_MAX_IDS = 60     # 一屏最多 40 张卡，60 留余量；防恶意超长查询
EXPLORE_STATUS_WORKERS = 8      # 与旧探索页实时集数并发度一致


@router.get('/api/explore/status', dependencies=[Depends(auth)])
def api_explore_status(ids: str = '', media: str = 'tv'):
    """探索第二段（§5.2）：对当前屏的 TMDB ID 并发现查 Emby 事实，就地更新卡片。

    - 有界 ThreadPoolExecutor 并发（撤回版串行 for 循环，一屏 40 卡在 Emby 慢时
      要把用户晾十几秒）；ids 上限 60。
    - 每卡返回 {fact, classification, data_time}（tv）或 {fact, data_time}（movie）；
      单卡失败返回 {status:'error', tmdb_id, message}——错误与「未入库」显式区分，
      不冒充。
    - 手动完结标记在此读出并传给 facts.get_series_status → classify（§5.3：
      classify 是唯一应用点；不传会把已完结的剧重新打回「缺集」）。
    """
    from concurrent.futures import ThreadPoolExecutor
    id_list = [x.strip() for x in (ids or '').split(',') if x.strip()][:EXPLORE_STATUS_MAX_IDS]
    if not id_list:
        return {'status': 'error', 'message': 'ids 为空'}
    if media not in ('tv', 'movie'):
        return {'status': 'error', 'message': 'media 必须是 tv 或 movie'}

    # 复审修正：完结标记的键是 Emby 条目 id——传整份 done dict，由
    # facts.get_series_status 在拿到 SeriesId 后按「Emby id / tmdb_id 双键位」匹配
    done = {}
    if media == 'tv':
        try:
            done = morning.read_manual_done() or {}
        except Exception as e:
            logging.getLogger('media_agent').warning('探索第二段读取手动完结失败: %s', e)

    def _one(tid):
        try:
            if media == 'movie':
                return facts.get_movie_status(tid)
            return facts.get_series_status(tid, manual_done_set=done)
        except Exception as e:            # EmbyFactError 及一切意外都显式化，不冒充「未入库」
            return {'status': 'error', 'tmdb_id': tid, 'message': str(e)}

    with ThreadPoolExecutor(max_workers=EXPLORE_STATUS_WORKERS) as pool:
        data = list(pool.map(_one, id_list))
    return {'status': 'success', 'media': media, 'data': data, 'count': len(data),
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime())}


@router.get('/api/emby/library', dependencies=[Depends(auth)])
def api_emby_library(force: int = 0, with_tmdb: int = 1, progress: int = 0):
    """片库映射数据源（S3 起废除 cache_only=1 口径——页打开读 /api/mapping）。

    - progress=1：只回后台对照进度快照（原 /api/tmdb/progress 收口于此）；
    - with_tmdb=0：快速模式，只拉 Emby 库，不查 TMDB；
    - 非 force：读 6 小时内的 TMDB 对照缓存；
    - force=1：启动后台对照任务，立即返回。
    """
    # 只回进度：前端对照进度条轮询用（S7：独立 progress 端点已并入）
    if progress:
        return {'status': 'success', 'progress': _tmdb_scan_progress_dict()}
    # 快速模式：不查 TMDB
    if not with_tmdb:
        return morning.action_emby_library(Args(force=False, with_tmdb=False))
    # 读缓存模式
    if not force:
        cached = morning.read_emby_lib_cache(max_age=6 * 3600)
        if cached:
            cached['from_cache'] = True
            return cached
    # force=1：启动后台任务，立即返回。
    # S7：get_tmdb_scan_progress 一套已并入这里——进度随本端点下发
    #（前端轮询本端点即可拿到 started/running 状态与进度），不再有单独接口。
    prog = _tmdb_scan_progress_dict()
    if prog.get('running'):
        return {'status': 'running', 'message': 'TMDB 对照已在后台运行', 'progress': prog}
    import threading as _th
    _th.Thread(target=morning.refresh_tmdb_scan, daemon=True).start()
    return {'status': 'started', 'message': 'TMDB 对照已在后台启动'}


@router.get('/api/mapping', dependencies=[Depends(auth)])
def api_mapping():
    """片库映射页秒开（§5.2/§4.3 D1）：立刻返回事实层已有数据 + 数据时间。

    - 有事实数据：返回 series/movies/stats（分类来自 facts.classify 的中文六态）
      与每条事实的 data_time；前端随即对屏幕内的剧走 /api/mapping/verify 现查更新；
    - 尚无任何事实：返回 {status:'success', empty:true, message:'尚未扫描'}——
      明确提示而不是 nocache 报错（废除旧 cache_only=1 的 24h 窗口口径）；
      全库统计的新鲜度仍靠后台 TMDB 对照（事实库由它写入）。
    """
    facts_rows = storage.db_facts_all('tv')
    if not facts_rows:
        return {'status': 'success', 'empty': True,
                'message': '尚未扫描：请点「重新对照」建立事实基线（后台运行，完成后自动刷新）'}
    series, movies = [], []
    for row in facts_rows:
        p = row['payload'] or {}
        cls = p.get('classification') or {}
        series.append({
            'id': (p.get('series_ids') or ['tv:' + str(row['tmdb_id'])])[0],
            'series_ids': p.get('series_ids') or [],
            'tmdb_id': row['tmdb_id'], 'name': p.get('series_name') or '',
            'in_local': bool(p.get('in_local')), 'in_share': bool(p.get('in_share')),
            'have_eps': p.get('have_total') or 0,
            'total_episodes': p.get('have_total') or 0,
            'tmdb_total': p.get('tmdb_total') or 0,
            'declared_total': p.get('declared_total') or 0,
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(row['updated_at'])),
            'tmdb_info': {
                'match_status': cls.get('status', 'unmatched'),
                'status_cn': cls.get('label', '未匹配'),
                'missing_ranges': cls.get('missing_ranges', ''),
                'extra_total': len(cls.get('extra') or []),
                'tmdb_total': p.get('tmdb_total') or 0,
                'declared_total': p.get('declared_total') or 0,
                'diff': (p.get('have_total') or 0) - (p.get('tmdb_total') or 0),
                'manual_done': bool(cls.get('manual_done')),
                'source': 'facts',
            },
        })
    for row in storage.db_facts_all('movie'):
        p = row['payload'] or {}
        movies.append({
            'id': (p.get('movie_ids') or ['movie:' + str(row['tmdb_id'])])[0],
            'ids': p.get('movie_ids') or [],
            'tmdb_id': row['tmdb_id'], 'name': p.get('name') or '',
            'in_local': bool(p.get('in_local')), 'in_share': bool(p.get('in_share')),
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(row['updated_at'])),
            'source': 'facts',
        })
    stats = morning._recompute_mapping_stats_dict(series)
    return {'status': 'success', 'empty': False, 'series': series, 'movies': movies,
            'stats': stats, 'ts': max(r['updated_at'] for r in facts_rows),
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(
                max(r['updated_at'] for r in facts_rows)))}


MAPPING_VERIFY_MAX = 30       # 屏幕内重验一次最多 30 部（40 卡页面可视区通常更少）
MAPPING_VERIFY_WORKERS = 8


@router.get('/api/mapping/verify', dependencies=[Depends(auth)])
def api_mapping_verify(ids: str = '', media: str = 'tv'):
    """屏幕内的剧打开/可见时后台重验（D1/§4.3）：并发现查 Emby 事实并回写事实库。

    与探索第二段同一条事实层路径（facts.get_*_status），手动完结标记必须携带，
    防止实时刷新把已完结的剧重新打回「缺集」。
    """
    from concurrent.futures import ThreadPoolExecutor
    id_list = [x.strip() for x in (ids or '').split(',') if x.strip()][:MAPPING_VERIFY_MAX]
    if not id_list:
        return {'status': 'error', 'message': 'ids 为空'}
    if media not in ('tv', 'movie'):
        return {'status': 'error', 'message': 'media 必须是 tv 或 movie'}

    done = {}
    if media == 'tv':
        try:
            done = morning.read_manual_done() or {}
        except Exception as e:
            logging.getLogger('media_agent').warning('映射重验读取手动完结失败: %s', e)

    def _one(tid):
        try:
            if media == 'movie':
                return facts.get_movie_status(tid)
            return facts.get_series_status(tid, manual_done_set=done)
        except Exception as e:            # EmbyFactError 及一切意外都显式化，不冒充「未入库」
            return {'status': 'error', 'tmdb_id': tid, 'message': str(e)}

    with ThreadPoolExecutor(max_workers=MAPPING_VERIFY_WORKERS) as pool:
        data = list(pool.map(_one, id_list))
    return {'status': 'success', 'media': media, 'data': data, 'count': len(data),
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime())}


@router.get('/api/library_stats', dependencies=[Depends(auth)])
def api_library_stats():
    try:
        return stats.action_library_stats(Args())
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/settings', dependencies=[Depends(auth)])
def api_get_ingest_settings():
    return {'status': 'success', 'settings': _cfg.get_ingest_cfg()}


@router.post('/api/ingest/settings', dependencies=[Depends(auth)])
def api_set_ingest_settings(body: dict = None):
    body = body or {}

    def _apply(cfg):
        if 'enabled' in body:
            cfg['ingest_enabled'] = '1' if body['enabled'] else '0'
        if 'interval_min' in body:
            try:
                v = max(1, min(1440, int(body['interval_min'])))
            except (ValueError, TypeError):
                v = 5
            cfg['ingest_interval_min'] = str(v)
    _cfg.update_config(_apply)
    state.reload_config()
    return {'status': 'success', 'settings': _cfg.get_ingest_cfg()}


@router.get('/api/emby/series/{series_id}/episodes', dependencies=[Depends(auth)])
def api_emby_series_episodes(series_id: str):
    """返回某剧所有分集（含 Path），前端可自行分库统计"""
    try:
        data = emby.emby_request('/Items', {
            'ParentId': series_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Episode',
            'Fields': 'Path,ParentIndexNumber,IndexNumber',
            'Limit': 5000,
        }) or {}
        episodes = []
        for ep in data.get('Items', []):
            path = ep.get('Path', '') or ''
            lib = state.emby_lib_of(path) or 'other'
            conv = emby.emby_path_to_container(path)
            exists = True if conv is None else conv.exists()   # None = 映射不上，无法核实
            episodes.append({
                'season': ep.get('ParentIndexNumber'),
                'episode': ep.get('IndexNumber'),
                'lib': lib,
                'path': path,
                'exists': exists,
            })
        return {'status': 'success', 'episodes': episodes}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _delete_title_files(files, target, title, media_type, series_id=None, tmdb_id=None):
    """单剧/单片删除的公共执行段：拿跨入口文件锁 → 删文件（与双库治理同一条 fail-safe 删除链：
    本地库=删 strm + 联动 CD2 源文件；分享库=只删 strm）→ 立即修补缓存（海报马上撤下）
    → 后台精准通知 Emby → 写审计。"""
    root = state.L_ROOT if target == 'local' else state.S_ROOT
    cloud = state.CLOUD_L_ROOT if target == 'local' else None
    try:
        with wash.mutation_lock():
            result = wash.safe_delete_files(files, root, cloud, dry_run=False)
    except wash.MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}

    # 实际已删掉的文件（fail-safe 可能因云端源文件找不到而保留 strm）→ Emby 侧路径；空目录一并通知
    gone = [f for f in files if not f.exists()]
    emby_paths = set()
    for f in gone:
        emby_paths.add(emby.container_to_emby_path(f, target))
        for d in (f.parent, f.parent.parent):
            if not d.exists():
                emby_paths.add(emby.container_to_emby_path(d, target))

    stats.invalidate_media_caches(keep_emby_lib=True)
    series_removed, entry = False, None
    try:
        if series_id is not None:
            info = morning.patch_emby_lib_cache_after_series_delete(series_id, target)
            series_removed = info['remaining'] == 0
            entry = info['entry']
            if series_removed and info['series_path']:
                emby_paths.add(info['series_path'])   # 整部剧都没了：连剧目录一起通知
        elif tmdb_id is not None:
            morning.patch_emby_lib_cache_after_movie_delete(tmdb_id, target)
    except Exception as e:
        state.log.warning('删除后缓存修补失败: %s', e)
    emby.notify_emby_deleted(emby_paths)    # 后台线程，不阻塞返回

    target_cn = '本地库' if target == 'local' else '分享库'
    cloud_note = '未处理云端源文件' if target == 'share' else ('删除 %d 个云端源文件' % result['cloud_removed'])
    governance.write_audit_log(
        '单剧删除',
        '《%s》删除%s：%d 个 strm' % (title, target_cn, result['strm_removed']),
        [
            '类型：%s' % media_type,
            '目标：%s' % target_cn,
            'strm 删除：%d 个' % result['strm_removed'],
            '云端源文件：%s' % cloud_note,
        ] + (['错误：%s' % e for e in result.get('errors', [])[:3]])
    )
    return {
        'status': 'success', 'dry_run': False, 'target': target,
        'count': len(files),
        'strm_removed': result['strm_removed'],
        'cloud_removed': result['cloud_removed'],
        'errors': result.get('errors', []),
        'series_removed': series_removed, 'entry': entry,
    }


@router.post('/api/emby/series/{series_id}/delete', dependencies=[Depends(auth)])
def api_emby_series_delete(series_id: str, body: dict = None):
    """删除某剧的全部文件
    target: local | share
    dry_run: True -> 只返回数量，不真删
    """
    body = body or {}
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    series_name = str(body.get('series_name', '')).strip() or '(未命名)'
    media_type = str(body.get('media_type', '')).strip() or '剧集'

    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    try:
        data = emby.emby_request('/Items', {
            'ParentId': series_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Episode',
            'Fields': 'Path',
            'Limit': 5000,
        }) or {}
        root = state.L_ROOT if target == 'local' else state.S_ROOT
        files = []
        for ep in data.get('Items') or []:
            conv = emby.emby_path_to_container(ep.get('Path') or '')
            if conv and wash._inside(conv, root) and conv.exists():
                files.append(conv)

        if not files:
            return {'status': 'success', 'count': 0, 'target': target,
                    'message': '该库无此剧文件', 'ghost': True}
        if dry_run:
            return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target}
        return _delete_title_files(files, target, series_name, media_type, series_id=series_id)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/series/{series_id}/resync', dependencies=[Depends(auth)])
def api_emby_series_resync(series_id: str):
    """不删任何文件：以磁盘真实文件为准，重算/移除片库映射缓存里的这部剧，并通知 Emby 刷新。
    用于清掉「文件已经没了但海报还在」的失效条目。"""
    try:
        info = morning.patch_emby_lib_cache_after_series_delete(series_id)
        stats.invalidate_media_caches(keep_emby_lib=True)
        if info['remaining'] == 0 and info['series_path']:
            emby.notify_emby_deleted([info['series_path']])
        return {'status': 'success', 'remaining': info['remaining'],
                'series_removed': info['remaining'] == 0, 'entry': info['entry']}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/movie/delete_by_tmdb', dependencies=[Depends(auth)])
def api_movie_delete(body: dict = None):
    """按 tmdb_id 删除电影（扫目录名，不依赖 Emby 索引）"""
    body = body or {}
    tmdb_id = str(body.get('tmdb_id', '')).strip()
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    movie_name = str(body.get('name', '')).strip() or '(未命名)'

    if not tmdb_id.isdigit():
        return {'status': 'error', 'message': '缺少 tmdb_id'}
    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    root = state.L_ROOT if target == 'local' else state.S_ROOT
    files = wash.find_movie_strms_by_tmdb(root, tmdb_id)
    if not files:
        return {'status': 'success', 'count': 0, 'target': target}
    if dry_run:
        return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target}
    return _delete_title_files(files, target, movie_name, '电影', tmdb_id=tmdb_id)
