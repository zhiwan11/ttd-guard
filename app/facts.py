# -*- coding: utf-8 -*-
"""统一事实层（白皮书 §5.1 / §5.3，阶段 S1）：Emby 取数、集合分类与 SQLite 存储。

四公开接口（所有页面共用，禁止各页再各写一套「Series → 分集」）：
- ``find_series(tmdb_id)``：身份识别三层依次兜底（AnyProviderIdEquals → 全量
  ProviderIds.Tmdb → 目录名 {tmdb-xxx}），继承 PR12 修掉的「服务端忽略过滤参数
  返回全库」误命中修复；
- ``series_episodes(series_ids)``：拉分集，展开 IndexNumberEnd，按库根拆
  local/share，S00 单独标记**不计缺集**，记录每集 DateCreated 供晨报用；
- ``movie_present(tmdb_id)``：{'in_local','in_share'}；
- ``classify(...)``：**全仓唯一**分类函数（§5.3 六态中文枚举 + 手动完结只在此应用）。

错误口径：``EmbyFactError``——Emby 不可达 / Key 为空 / 超时一律上抛，
**绝不返回空集合冒充「没有」**（那会把整库误标成「未入库」）。与 S0 引入的
``emby.EmbyError`` 同源同语义，本模块对其统一收口为 EmbyFactError（RuntimeError 子类）。

存储：``storage`` 的 media_facts 表（ttd-guard.db，WAL）。分集键统一「S01E02」
字符串——JSON 往返安全（撤回版曾用元组键，经 json.dumps 变成列表，恢复时被误判）。

编排（S1 阶段消费方以薄封装逐步接入；S3 片库映射、S2 探索两段式建成后全量切换）：
- ``get_series_status`` / ``get_movie_status``：取数 + classify + 落盘 + data_time，
  同一条事实**只写一次库**；
- ``refresh_screen_facts``：屏幕内卡片并发重验（有界 ThreadPoolExecutor，不串行）。

只依赖 state / emby / lib / tmdb / media / storage 等既有模块，不反向依赖业务模块。
"""
import logging
import re
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import threading

from . import state, emby, lib, storage, media

log = logging.getLogger('media_agent')

ITEMS_LIMIT = 50000          # 单次 /Items 上限（与 media.EP_LIMIT 同口径）
SCREEN_WORKERS = 4           # refresh_screen_facts 的并发度（bot 更新处理同为 4 线程）


class EmbyFactError(RuntimeError):
    """Emby 不可达 / Key 为空 / 请求失败 / 超时：事实层统一错误出口。

    取数失败**必须上抛**，绝不返回空集合冒充「没有」——那是「Emby 短暂异常把
    整库打成未入库」这类静默归零缺陷的根源。调用方决定怎么提示（显式报错 /
    保留旧卡片 / 计入失败明细）。
    """


# ── 分集键约定：'S01E02' 字符串（JSON 往返安全；见模块 docstring） ──

def ep_key(sn, en):
    """(季, 集) -> 'S01E02'；S00（特别篇，sn=0）也产出键（series_episodes 单独标记用）；
    集/季非正返回 None。"""
    try:
        sn, en = int(sn), int(en)
    except (TypeError, ValueError):
        return None
    if sn < 0 or en <= 0:
        return None
    return 'S%02dE%02d' % (sn, en)


def parse_ep_key(k):
    """'S01E02' -> (1, 2)；解析不了返回 None。"""
    m = re.match(r'S(\d+)E(\d+)$', str(k or ''))
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_ep_ranges(eps):
    """{'S01E26','S01E27','S01E28',...} -> 精确区间串（§5.3「S01E26–E28」口径）。

    同季连续合并为 'S01E26–E28'，单集保留 'S01E26'，跨季 ', ' 连接；超过 6 段截断。
    与 tgmsg/blocks.ep_ranges 同一排版口径（那边吃 (季,集) 元组，这边吃键字符串）。
    """
    items = sorted(e for e in (parse_ep_key(k) for k in (eps or [])) if e)
    if not items:
        return ''
    parts, i, n = [], 0, len(items)
    while i < n:
        sn, lo = items[i]
        hi, j = lo, i + 1
        while j < n and items[j][0] == sn and items[j][1] == hi + 1:
            hi, j = items[j][1], j + 1
        parts.append('S%02dE%02d' % (sn, lo) if hi == lo else 'S%02dE%02d–E%02d' % (sn, lo, hi))
        i = j
    if len(parts) > 6:
        parts = parts[:6] + ['…等 %d 集' % n]
    return ', '.join(parts)


# ═══════════════════ ① find_series：身份识别（三层兜底，全仓唯一） ═══════════════════

def _pid(item):
    """ProviderIds 里的 TMDB id（键大小写都认）；没有返回 ''。与 media._pid 同口径。"""
    return media._pid(item)


def _series_items_exact(want):
    """第一层：AnyProviderIdEquals 精确查询。

    正式 Emby 服务端可能忽略该过滤参数返回**全库**——必须逐条校验
    ProviderIds.Tmdb（PR12 修掉的误命中缺陷，这里必须保留）。请求失败抛
    EmbyFactError，不返回空列表冒充「没有」。
    """
    data = emby.emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Name,Path', 'Limit': ITEMS_LIMIT,
        'AnyProviderIdEquals': 'Tmdb.%s' % want,
    }) or {}
    return [it for it in (data.get('Items') or [])
            if it.get('Id') and _pid(it) == want]


def _series_items_all(want):
    """第二层：全量 Series 清单按 ProviderIds.Tmdb 过滤。"""
    data = emby.emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Name,Path', 'Limit': ITEMS_LIMIT,
    }) or {}
    return [it for it in (data.get('Items') or [])
            if it.get('Id') and _pid(it) == want]


def _series_items_by_dirname(want):
    """第三层：目录名 {tmdb-xxx}（上游 TgtoDrive 把 tmdb 写在目录名里，Emby 刮不进
    ProviderIds）。用 lib._disk_tmdb_lookup 的磁盘索引定位目录，再按容器路径前缀
    匹配 Emby Series 条目；仍是请求失败上抛、目录对不上返回空列表。"""
    disk_lookup = lib._disk_tmdb_lookup()
    paths = [str(p) for p, v in (disk_lookup or {}).items() if str(v or '') == want]
    if not paths:
        return []
    data = emby.emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Name,Path', 'Limit': ITEMS_LIMIT,
    }) or {}
    rows = []
    for it in (data.get('Items') or []):
        if not it.get('Id'):
            continue
        conv = emby.emby_path_to_container(it.get('Path') or '')
        if conv is None:
            continue
        c = str(conv)
        for p in paths:
            if c == p or c.startswith(p.rstrip('/\\') + '/') or p.startswith(c.rstrip('/\\') + '/'):
                rows.append(it)
                break
    return rows


def find_series(tmdb_id):
    """按 TMDB id 找到 Emby Series 条目列表（原始 Item dict，含 Id/Name/Path/ProviderIds）。

    三层依次兜底（§5.1）：① AnyProviderIdEquals（服务端可能忽略过滤参数 → 逐条校验
    ProviderIds.Tmdb）→ ② 全量清单按 ProviderIds.Tmdb 过滤 → ③ 目录名 {tmdb-xxx}。
    所有层都查询成功但没匹配 → 返回 []（这是诚实的「未入库」）；
    任何一层请求失败 → 抛 EmbyFactError（不能把 Emby 故障当成未入库）。
    """
    want = str(tmdb_id or '').strip()
    if not want:
        return []
    try:
        items = _series_items_exact(want)
    except Exception as e:
        raise EmbyFactError('Emby Series 精确查询失败 %s: %s' % (want, e)) from e
    if items:
        return items
    try:
        items = _series_items_all(want)
    except Exception as e:
        raise EmbyFactError('Emby Series 全量查询失败 %s: %s' % (want, e)) from e
    if items:
        return items
    try:
        return _series_items_by_dirname(want)
    except Exception as e:
        raise EmbyFactError('Emby Series 目录名兜底查询失败 %s: %s' % (want, e)) from e


def series_ids_by_tmdb(tmdb_id):
    """find_series 的 id 列表形态。"""
    return [it['Id'] for it in find_series(tmdb_id) if it.get('Id')]


# ═══════════════════ ② series_episodes：分集集合（按库根拆分，S00 单独标记） ═══════════════════

def series_episodes(series_ids):
    """若干 Emby Series 条目的分集并集。

    返回 dict（键集合一律用 'S01E02' 字符串约定）：
      - episodes：正式分集（S01+，落在两个库根内或无法核实路径的）键集合；
      - local / share：按 ``state.emby_lib_of`` 拆到库根的子集；
      - special：S00（特别篇）键集合——**单独标记，不计入缺集**（§5.1/§5.3）；
      - created：键 -> DateCreated（同集取较大值，晨报「24h 入库」口径复用）；
      - series_ids / series_name / name：来源条目信息。

    IndexNumberEnd（一集连播多集）展开成区间；分集请求失败抛 EmbyFactError。
    """
    ids = []
    for s in (series_ids or []):
        sid = s.get('Id') if isinstance(s, dict) else s
        if sid:
            ids.append(sid)
    out = {'episodes': set(), 'local': set(), 'share': set(), 'special': set(),
           'created': {}, 'series_ids': ids, 'series_name': ''}
    names = []
    for sid in ids:
        try:
            data = emby.emby_request('/Items', {
                'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,DateCreated,Path',
                'Limit': ITEMS_LIMIT,
            }) or {}
        except Exception as e:
            raise EmbyFactError('Emby 分集查询失败（Series %s）: %s' % (sid, e)) from e
        for ep in (data.get('Items') or []):
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                en_end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if en <= 0:
                continue
            path = ep.get('Path') or ''
            lib_tag = state.emby_lib_of(path) if path else ''
            # 路径能归属到配置的库根才计入 local/share；映射不到库根（其它库/无路径）
            # 仍保留在 episodes 里——「存在这一集」是事实，只是归属未核实。
            for n in range(en, max(en, en_end) + 1):
                k = ep_key(sn, n)
                if not k:
                    continue
                if sn == 0:
                    out['special'].add(k)       # S00：单独标记，不进 episodes（不计缺集）
                    continue
                out['episodes'].add(k)
                if lib_tag == 'local':
                    out['local'].add(k)
                elif lib_tag == 'share':
                    out['share'].add(k)
                out['created'][k] = max(out['created'].get(k, ''), ep.get('DateCreated') or '')
    # 系列名从原始条目取（第一个有 Name 的）
    for s in (series_ids or []):
        if isinstance(s, dict) and s.get('Name') and not out['series_name']:
            out['series_name'] = s.get('Name')
    return out


# ═══════════════════ ③ movie_present：电影在库 ═══════════════════

def movie_present(tmdb_id):
    """按 TMDB id 查电影在库情况 → {'in_local','in_share','matched','movie_ids','name'}。

    与 find_series 同一条身份兜底链（AnyProviderIdEquals → 全量 ProviderIds → 磁盘）；
    请求失败抛 EmbyFactError。
    """
    want = str(tmdb_id or '').strip()
    out = {'in_local': False, 'in_share': False, 'matched': False, 'movie_ids': [], 'name': ''}
    if not want:
        return out

    def _query(extra):
        data = emby.emby_request('/Items', dict({
            'Recursive': 'true', 'IncludeItemTypes': 'Movie',
            'Fields': 'ProviderIds,Name,Path', 'Limit': ITEMS_LIMIT,
        }, **extra)) or {}
        return data.get('Items') or []

    rows = []
    try:
        rows = [it for it in _query({'AnyProviderIdEquals': 'Tmdb.%s' % want})
                if it.get('Id') and _pid(it) == want]
    except Exception as e:
        raise EmbyFactError('Emby 电影精确查询失败 %s: %s' % (want, e)) from e
    if not rows:
        try:
            rows = [it for it in _query({}) if it.get('Id') and _pid(it) == want]
        except Exception as e:
            raise EmbyFactError('Emby 电影全量查询失败 %s: %s' % (want, e)) from e
    if not rows:
        # 目录名兜底：磁盘索引里这个 tmdb 对应的目录下有电影条目
        try:
            disk_lookup = lib._disk_tmdb_lookup()
            paths = {str(p) for p, v in (disk_lookup or {}).items() if str(v or '') == want}
            rows = []
            for it in _query({}):
                if not it.get('Id'):
                    continue
                conv = emby.emby_path_to_container(it.get('Path') or '')
                if conv is not None and any(str(conv) == p or str(conv).startswith(p.rstrip('/\\') + '/')
                                            for p in paths):
                    rows.append(it)
        except Exception as e:
            raise EmbyFactError('Emby 电影目录名兜底查询失败 %s: %s' % (want, e)) from e
    if not rows:
        return out                       # 查询都成功 → 诚实的「未入库」
    for it in rows:
        tag = state.emby_lib_of(it.get('Path') or '')
        if tag == 'local':
            out['in_local'] = True
        elif tag == 'share':
            out['in_share'] = True
        out['movie_ids'].append(it['Id'])
    out['matched'] = True
    out['name'] = next((it.get('Name') for it in rows if it.get('Name')), '')
    return out


# ═══════════════════ ④ classify：全仓唯一分类函数（§5.3 六态） ═══════════════════

STATUS_LABELS = {
    'complete': '完整', 'missing': '缺集', 'extra': '超集',
    'ongoing': '在更', 'ongoing_lagging': '在更·落后', 'unmatched': '未匹配',
}
_ONGOING_STATUSES = ('Returning Series', 'In Production', 'Planned', 'Pilot')


def classify(have, tmdb_info, manual_done=False):
    """唯一的分类函数（§5.3 表）：输入集号集合，输出六态之一 + 精确缺集区间。

    参数：
      - have：Emby 现有分集键集合（'S01E02' 字符串；（季,集）元组也接受，
        S00 不应包含在内——调用方用 series_episodes 的 episodes 键）；
      - tmdb_info：``tmdb._tmdb_series_info`` 形态（aired 为 (季,集) 集合、
        status、total_episodes、declared_total）；None → 未匹配；
      - manual_done：手动完结标记——**只在这一处应用**（白皮书 §5.3：不是映射、
        晨报各处理一遍）。

    返回 dict：{'status', 'label', 'missing', 'missing_ranges', 'extra',
    'have_total', 'aired_total', 'tmdb_status', 'ongoing', 'manual_done'}。

    判定顺序（互斥，自上而下）：手动完结 → 未匹配 → 缺集/在更·落后（aired−have）→
    在更 → 超集（只在已完结时判定）→ 完整。集合差集，不数集数——避免「缺一个、
    多一个」互相抵消。
    """
    have = _official_keys(have)
    result = {'missing': [], 'missing_ranges': '', 'extra': [], 'have_total': len(have),
              'aired_total': 0, 'tmdb_status': '', 'ongoing': False, 'manual_done': bool(manual_done)}
    if manual_done:
        # 手动完结：视为完整；aired 仍给出（弹窗显示原始明细用）
        result.update({'status': 'complete', 'label': '完整',
                       'aired_total': len(_official_keys((tmdb_info or {}).get('aired')))})
        return result
    if not tmdb_info:
        result.update({'status': 'unmatched', 'label': '未匹配'})
        return result
    aired = _official_keys((tmdb_info or {}).get('aired'))
    tmdb_status = str(tmdb_info.get('status') or '')
    ongoing = tmdb_status in _ONGOING_STATUSES
    result.update({'aired_total': len(aired), 'tmdb_status': tmdb_status, 'ongoing': ongoing})
    missing = aired - have
    extra = have - aired
    if missing:
        if ongoing:
            # D3：在更但已入库少于已播 → 单独一档「在更·落后」（晨报缺集榜默认不含）
            result.update({'status': 'ongoing_lagging', 'label': '在更·落后'})
        else:
            result.update({'status': 'missing', 'label': '缺集'})
    elif ongoing:
        result.update({'status': 'ongoing', 'label': '在更'})
    elif extra:
        # 超集只在剧已完结时判定（§5.3：在更剧里 TMDB 没有的集多半是还没播）
        result.update({'status': 'extra', 'label': '超集'})
    else:
        result.update({'status': 'complete', 'label': '完整'})
    result['missing'] = sorted(missing)
    result['missing_ranges'] = fmt_ep_ranges(missing)
    result['extra'] = sorted(extra)
    return result


def _as_keys(eps):
    """归一分集键：'S01E02' 字符串原样；(季,集) 元组转键；非法丢弃。"""
    out = set()
    for e in (eps or []):
        if isinstance(e, str):
            if parse_ep_key(e):
                out.add(e)
        else:
            k = ep_key(*e) if isinstance(e, (tuple, list)) and len(e) == 2 else None
            if k:
                out.add(k)
    return out


def _official_keys(eps):
    """正式分集键（S01+）：S00（特别篇）一律剔除——§5.1「S00 单独标记，不计入
    缺集」，§5.3「超集……特别篇不计」。classify 的差集只在这批键上进行。"""
    return {k for k in _as_keys(eps) if not k.startswith('S00')}


# ═══════════════════ 编排：取数 + classify + 落盘（同一条事实只写一次库） ═══════════════════

def _fact_key(media_type, tmdb_id):
    return '%s:%s' % (media_type, str(tmdb_id))


def _persist_fact(media_type, tmdb_id, payload, source='emby'):
    """写一条事实并返回完整行（含 updated_at）。"""
    key = _fact_key(media_type, tmdb_id)
    ok = storage.db_fact_put(key, media_type, str(tmdb_id), payload, source)
    if not ok:
        log.warning('事实落盘失败（内存返回，不阻断）: %s', key)
    row = storage.db_fact_get(key) or {}
    return {'fact_key': key, 'updated_at': row.get('updated_at') or time.time(),
            'source': row.get('source') or source}


def get_series_status(tmdb_id, manual_done=False, tmdb_info=None, persist=True):
    """某剧当前事实：取数（find_series + series_episodes）+ classify + 落盘。

    - tmdb_info 可由调用方传入（同屏批量避免重复请求 TMDB）；
      缺省用 tmdb._tmdb_series_info（24h 库缓存）。
    - 同一条事实**只写一次库**：分类结果合并进同一份 payload 一次写入
      （撤回版曾先写取数结果、classify 后再写一遍，双写浪费且可能读到中间态）。
    - Emby 取数失败抛 EmbyFactError；TMDB 查询失败由 _tmdb_series_info 返回 None
      （分类为未匹配——TMDB 网络问题按白皮书 §5.2 的既定口径处理）。
    """
    tid = str(tmdb_id or '').strip()
    info = tmdb_info if tmdb_info is not None else _load_tmdb_info(tid)
    try:
        rows = find_series(tid)
        eps = series_episodes(rows)
    except EmbyFactError:
        raise
    have = eps['episodes']
    cls = classify(have, info, manual_done=manual_done)
    payload = {
        'media_type': 'tv', 'tmdb_id': tid, 'matched': bool(rows),
        'series_ids': eps['series_ids'], 'series_name': eps['series_name'],
        'episodes': sorted(have), 'local': sorted(eps['local']), 'share': sorted(eps['share']),
        'special': sorted(eps['special']), 'created': eps['created'],
        'in_local': bool(eps['local']), 'in_share': bool(eps['share']),
        'have_total': len(have), 'tmdb_total': (info or {}).get('total_episodes') or 0,
        'declared_total': (info or {}).get('declared_total') or 0,
        'classification': cls,
    }
    saved = _persist_fact('tv', tid, payload) if persist else {'fact_key': _fact_key('tv', tid), 'updated_at': time.time(), 'source': 'emby'}
    return {'status': 'success', 'ok': True, 'fact': payload, 'classification': cls,
            'fact_key': saved['fact_key'], 'updated_at': saved['updated_at'],
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(saved['updated_at']))}


def get_movie_status(tmdb_id, persist=True):
    """某电影当前事实：movie_present + 落盘（一次写入）。"""
    tid = str(tmdb_id or '').strip()
    present = movie_present(tid)
    payload = {'media_type': 'movie', 'tmdb_id': tid, 'matched': present['matched'],
               'movie_ids': present['movie_ids'], 'name': present['name'],
               'in_local': present['in_local'], 'in_share': present['in_share']}
    saved = _persist_fact('movie', tid, payload) if persist else {'fact_key': _fact_key('movie', tid), 'updated_at': time.time(), 'source': 'emby'}
    return {'status': 'success', 'ok': True, 'fact': payload,
            'fact_key': saved['fact_key'], 'updated_at': saved['updated_at'],
            'data_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(saved['updated_at']))}


def refresh_screen_facts(items, persist=True):
    """并发重验一屏卡片（§5.2 第二段）：items = [{'tmdb_id','type'|'media_type'}]。

    有界 ThreadPoolExecutor（SCREEN_WORKERS），不串行（撤回版逐个同步请求，
    一屏 20 张卡在 Emby 慢时要把用户晾十几秒）。单卡失败进该卡的 error 字段，
    不拖垮整屏——但错误是显式的（'error': 原因），不是「未入库」。
    """
    jobs = []
    for it in (items or []):
        tid = str((it or {}).get('tmdb_id') or (it or {}).get('id') or '').strip()
        mtype = str((it or {}).get('type') or (it or {}).get('media_type') or 'tv')
        if tid:
            jobs.append((tid, mtype))
    if not jobs:
        return []

    def _one(job):
        tid, mtype = job
        try:
            return get_movie_status(tid, persist=persist) if mtype == 'movie' else get_series_status(tid, persist=persist)
        except EmbyFactError as e:
            return {'status': 'error', 'ok': False, 'tmdb_id': tid, 'media_type': mtype,
                    'error': str(e), 'reason': str(e)}
        except Exception as e:                     # 编程错误也显式化，不让一张卡悄悄消失
            log.warning('事实重验异常 %s: %s: %s', tid, type(e).__name__, e)
            return {'status': 'error', 'ok': False, 'tmdb_id': tid, 'media_type': mtype,
                    'error': str(e), 'reason': str(e)}

    with ThreadPoolExecutor(max_workers=SCREEN_WORKERS) as pool:
        return list(pool.map(_one, jobs))


# ═══════════════════ 已看进度（S4 / D4，白皮书 §5.4） ═══════════════════

WATCH_WORKERS = 4            # 逐用户播放记录查询的并发度
WATCH_ROUND_TTL = 600        # 单轮结果缓存：一轮追更检查（或一次批量订阅）内复用

_watch_cache = {}            # {'ts': float, 'data': {tmdb_id: {'episode':(sn,en),'date_played':...,'user':...} | None}}
_watch_lock = threading.Lock()


def invalidate_watch_cache():
    """单轮缓存失效：新一轮批量检查开始时调用（避免跨轮使用陈旧播放记录）。"""
    with _watch_lock:
        _watch_cache.clear()


def _user_episode_watched(uid, series_ids):
    """某用户在这些 Emby Series 里看过（IsPlayed）的最高 (季, 集)。"""
    data = emby.emby_request('/Users/%s/Items' % uid, {
        'Recursive': 'true', 'IncludeItemTypes': 'Episode', 'Filters': 'IsPlayed',
        'Fields': 'ParentIndexNumber,IndexNumber,DatePlayed,SeriesId',
        'SortBy': 'DatePlayed', 'SortOrder': 'Descending', 'Limit': ITEMS_LIMIT,
    }) or {}
    best = None
    for it in (data.get('Items') or []):
        if str(it.get('SeriesId') or '') not in series_ids:
            continue
        try:
            pair = (int(it.get('ParentIndexNumber') or 0), int(it.get('IndexNumber') or 0))
        except (TypeError, ValueError):
            continue
        if pair[0] <= 0 or pair[1] <= 0:
            continue
        if best is None or pair > best['episode']:
            best = {'episode': pair, 'date_played': it.get('DatePlayed') or ''}
    return best


def watched_progress(tmdb_id, use_round_cache=True):
    """某剧的「已看进度」（自动来源）：所有 Emby 用户播放记录里的最高已播集。

    - 有界并发（WATCH_WORKERS）：逐用户 /Users/{uid}/Items IsPlayed 现查；
      **单轮结果缓存**（WATCH_ROUND_TTL）：一轮批量里多个订阅/重复查询不打 Emby——
      规避撤回版「每订阅每轮都全量查 /Users」把 Emby 打挂的坑；
    - 没有任何用户的播放记录 → None（诚实：没看过 ≠ 查不到）；
    - Emby 请求失败 → EmbyFactError（与事实层同口径，不冒充「没看过」）。
    """
    tid = str(tmdb_id or '').strip()
    if not tid:
        return None
    if use_round_cache:
        with _watch_lock:
            hit = _watch_cache.get(tid)
            if hit and time.time() - hit[0] < WATCH_ROUND_TTL:
                return hit[1]
    try:
        rows = find_series(tid)
    except EmbyFactError:
        raise
    series_ids = {str(r.get('Id')) for r in rows if r.get('Id')}
    if not series_ids:
        result = None
    else:
        try:
            users = emby.emby_request('/Users', {'Limit': 500}) or []
        except Exception as e:
            raise EmbyFactError('Emby 用户清单查询失败: %s' % e) from e

        def _one(u):
            uid = (u or {}).get('Id')
            if not uid:
                return None
            try:
                return _user_episode_watched(uid, series_ids)
            except Exception as e:
                raise EmbyFactError('Emby 播放记录查询失败（用户 %s）: %s' % (u.get('Name') or uid, e)) from e

        with ThreadPoolExecutor(max_workers=WATCH_WORKERS) as pool:
            per_user = list(pool.map(_one, users))
        best = None
        for r in per_user:
            if r and (best is None or r['episode'] > best['episode']):
                best = r
        result = best
    if use_round_cache:
        with _watch_lock:
            _watch_cache[tid] = (time.time(), result)
    return result


def effective_watched(manual_value, auto_value):
    """D4 并用、手动优先：生效已看 = 手动值（非空）否则自动值。

    manual_value 是订阅里的 ``watched_ep_manual``（'S01E15' 或 (1,15)）；手动值
    解析不了（格式坏）也回退自动值——手填错了不能把通知整个弄哑。
    返回 (季, 集) 元组或 None。
    """
    manual = None
    if isinstance(manual_value, str):
        manual = parse_ep_key(manual_value)
    elif isinstance(manual_value, (tuple, list)) and len(manual_value) == 2:
        try:
            p = (int(manual_value[0]), int(manual_value[1]))
            manual = p if p[0] > 0 and p[1] > 0 else None
        except (TypeError, ValueError):
            manual = None
    if manual:
        return manual
    return auto_value


# ═══════════════════ 读回（供 S2/S3 切换时使用；payload 往返保持键形态） ═══════════════════

def load_fact(media_type, tmdb_id):
    """读一条已落盘事实（payload 原样 dict + updated_at/source/fact_key）；没有返回 None。"""
    row = storage.db_fact_get(_fact_key(media_type, tmdb_id))
    if not row:
        return None
    out = dict(row['payload'] or {})
    out['updated_at'] = row['updated_at']
    out['source'] = row['source']
    out['fact_key'] = row['fact_key'] if 'fact_key' in row else _fact_key(media_type, tmdb_id)
    return out


def _load_tmdb_info(tid):
    """TMDB 已播集合与状态（24h 库缓存）；查询失败/无 Key 返回 None（→ 未匹配）。"""
    from . import tmdb as _tmdb
    return _tmdb._tmdb_series_info(tid)
