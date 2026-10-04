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
from . import state, tmdb, emby, lib, storage, tg, media, sync, facts
from .tgmsg import blocks as tgmsg_blocks, reports as tgmsg_reports

log = logging.getLogger('media_agent')


def get_subscriptions() -> list:
    """订阅列表（SQLite subscriptions 表，按保存顺序）。规范接口：config.get_subscriptions 只是兼容壳。"""
    return storage.db_subs_list()


def set_subscriptions(subs: list) -> list:
    """整份替换订阅列表，返回实际保存的列表。"""
    return storage.db_subs_replace(subs)


def _load_sub_state() -> dict:
    """读取订阅状态（SQLite sub_state 表，store = state.SUB_STORE）。
    旧版「删除状态文件即重置」的语义改为 reset_sub_state()。"""
    return storage.db_load_sub_state(state.SUB_STORE)


load_sub_state = _load_sub_state  # 公开名（bot / 路由用这个）

def _save_sub_state(sub_state: dict):
    sync.bump('subscriptions', invalidate=False, reason='sub_state')
    if not storage.db_save_sub_state(state.SUB_STORE, sub_state):
        log.warning('保存追更状态失败')

def reset_sub_state():
    """清空全部追更状态（下一轮检查按首次运行处理）。"""
    return storage.db_clear_sub_state(state.SUB_STORE)

def _save_subscription_report(updates):
    """保存最近一次真正成功推送的追更汇报（SQLite 文档 subscription_report）。晨报只引用这份实际汇报。"""
    payload = {'ts': time.time(), 'date': time.strftime('%Y-%m-%d', time.localtime()), 'updates': updates or []}
    if not storage.db_doc_put(storage.DOC_SUB_REPORT, payload):
        log.warning('保存追更实际汇报失败')

def _load_subscription_report(today_only=False):
    data = storage.db_doc_get(storage.DOC_SUB_REPORT)
    if not isinstance(data, dict): return None
    if today_only and data.get('date') != time.strftime('%Y-%m-%d', time.localtime()): return None
    return data

def _emby_series_latest_ep(series_tmdb_id: str):
    """查 Emby 里某剧（按 tmdb_id）的所有副本并 union 分集（S4 起走事实层取数）。

    同一剧可能同时存在本地/分享两个 Series 条目；对所有匹配 Series 聚合 (季,集)。
    数据访问统一走 facts（find_series 三层身份兜底 + series_episodes 展开与库归属，
    含 ProviderIds 校验与幽灵过滤），这里只把结果整理成追更状态机需要的形状；
    追更要求「现查现报」，不走 15 秒微缓存；完全查不到分集时返回 None，
    交由 _disk_series_eps 兜底。
    """
    rows = facts.find_series(series_tmdb_id)
    if not rows:
        return None
    eps = facts.series_episodes(rows)
    have = _keys_to_eps(eps['episodes'])        # 'S01E02' 键 → (季,集) 元组（状态机口径）
    if not have:
        return None
    created = eps.get('created') or {}
    sn, en = max(have)
    created_pairs = {}
    for k, v in created.items():
        p = facts.parse_ep_key(k)
        if p:
            created_pairs[p] = v
    return {
        'series_id': (eps.get('series_ids') or [''])[0] or None,
        'series_ids': list(eps.get('series_ids') or []),
        'series_name': eps.get('series_name') or '',
        'season': sn, 'episode': en,
        'date_created': created_pairs.get((sn, en), ''),
        'episodes': have,
    }

def _disk_series_eps(series_tmdb_id: str):
    """追更磁盘兜底：按目录名 tmdb 定位剧集分集（不依赖 Emby ProviderIds.Tmdb）。

    上游 TgtoDrive 把 tmdb 写在目录名 `{tmdb-xxx}` 里，而 Emby 的刮削器不会把
    目录名里的 tmdb 后缀解析进 ProviderIds，导致 ``_emby_series_latest_ep`` 按
    ``ProviderIds.Tmdb`` 查不到 → 追更拿不到任何分集、「不能用」。
    S4（P1-D）：改走 lib._disk_eps_by_tmdb 的磁盘事实直查（不走 media 的
    库清单缓存，也不经 Emby）；返回结构与 ``_emby_series_latest_ep`` 一致，
    多一个 ``source='disk'`` 便于日志区分。
    """
    if not series_tmdb_id:
        return None
    try:
        agg = lib._disk_eps_by_tmdb().get(str(series_tmdb_id))
    except Exception as e:
        log.warning('追更磁盘兜底扫描失败 %s: %s', series_tmdb_id, e)
        agg = None
    if not agg or not agg.get('episodes'):
        return None
    eps = set(agg['episodes'])                 # {(季,集)}，与状态机口径一致
    names = [agg.get('name') or ''] if agg.get('name') else []
    sn, en = max(eps)
    return {
        'series_id': None, 'series_ids': [],
        'series_name': names[0] if names else '',
        'season': sn, 'episode': en,
        'date_created': '', 'episodes': eps, 'source': 'disk',
    }

def _ep_key_num(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)) * 10000 + int(m.group(2))) if m else 0

def _ep_key(sn, en):
    """(1, 7) -> 'S01E07'"""
    return f'S{int(sn):02d}E{int(en):02d}'

def _parse_ep_key(k):
    """'S01E07' -> (1, 7)；解析不了返回 None"""
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)), int(m.group(2))) if m else None

def _eps_to_keys(eps):
    """[(季,集)] -> {'S01E01', ...}"""
    return {_ep_key(sn, en) for sn, en in (eps or []) if sn > 0 and en > 0}

def _keys_to_eps(keys):
    """{'S01E01', ...} -> {(季,集)}（忽略解析不出的字符串）"""
    out = set()
    for k in (keys or []):
        p = _parse_ep_key(k)
        if p:
            out.add(p)
    return out


keys_to_eps = _keys_to_eps  # 公开名（bot 用这个）

_fmt_ep_ranges = tgmsg_blocks.ep_ranges  # 实现已移到 app/tgmsg/blocks.py，这里保留旧名兼容

_CHECK_LOCK = threading.Lock()
CHECK_LOCK_TIMEOUT = 300


def check_subscriptions(send_notify=True, self_delivered=False) -> dict:
    """追更订阅检查入口：全局串行化，避免定时 / 网页 / bot 同时检查重复推送。

    第二个调用者最多等 CHECK_LOCK_TIMEOUT 秒；拿到锁后基于第一个调用者已保存的
    状态重新计算，通常得到「无变化」，因此不会重复发通知。等待超时返回 status='busy'。

    「发送」与「记账」是两个正交开关（白皮书 §5.4 修复1）：
      - send_notify=True：把更新推送到 Telegram，**发送成功才记账**；发送失败本轮
        回退，下一轮自动重试；
      - send_notify=False + self_delivered=True：调用方显式声明「结果已自行送达」
        （Bot /sub check 把结果回显给你）——本轮只算不落盘，返回值带 ``staged``，
        调用方在实际送达成功后调 ``commit_sub_check()`` 记账；
      - send_notify=False + self_delivered=False（默认）：只读预览，不推送、绝不落盘。
    """
    if not _CHECK_LOCK.acquire(timeout=CHECK_LOCK_TIMEOUT):
        log.warning('追更订阅检查繁忙：等待超过 %d 秒，本轮跳过', CHECK_LOCK_TIMEOUT)
        return {'updates': [], 'total': 0, 'status': 'busy', 'checked': 0, 'errors': []}
    try:
        return _run_subscription_check(send_notify, self_delivered)
    finally:
        _CHECK_LOCK.release()


def commit_sub_check(staged: dict) -> bool:
    """把一次「已自行送达」的订阅检查结果落盘（Bot /sub check 回显发送成功后调用）。

    ``check_subscriptions(self_delivered=True)`` 只算不写；调用方确认自己已经把结果
    发出去之后，用这里把 staged 状态合并进订阅状态 —— 「自己送达了才可记账」。
    回显发送失败时不调用，下一轮检查会把同一批集再报一遍（不吞通知）。
    """
    if not staged:
        return False
    state = _load_sub_state()
    state.update(staged)
    _save_sub_state(state)
    return True


def seed_new_subscriptions(subs: list) -> dict:
    """新订阅播种：把当前 Emby 已有集写入 notified_episodes 作基线（防刷屏）。

    保存订阅不再触发整轮检查（白皮书 §5.4 修复2），只对「新增订阅」做一次基线；
    已有订阅的状态一律不动。播种失败的订阅没有基线，下一轮检查的状态机会把它
    当全新订阅安静播种（不会刷屏），但基线时间点会晚于订阅时刻——所以失败必须
    显式返回给调用方提示，而不是吞掉。
    """
    seeded, errors = 0, []
    if not subs:
        return {'seeded': seeded, 'errors': errors}
    sub_state = _load_sub_state()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for sub in subs:
        sid = sub.get('id') or sub.get('tmdb_id') or sub.get('name')
        tmdb_id = sub.get('tmdb_id')
        if not sid or not tmdb_id:
            continue
        if (sub_state.get(sid) or {}).get('notified_episodes') is not None:
            continue    # 已有基线（或旧结构待状态机迁移），不重复播种
        try:
            # 与检查状态机同一条身份兜底链：Emby ProviderIds → 磁盘目录名 tmdb
            latest = _emby_series_latest_ep(tmdb_id) or _disk_series_eps(tmdb_id)
        except (emby.EmbyError, facts.EmbyFactError) as e:
            errors.append({'sid': sid, 'name': sub.get('name') or tmdb_id, 'message': str(e)})
            continue
        eps = (latest or {}).get('episodes') or set()
        keys = sorted(_eps_to_keys(eps))
        sub_state[sid] = {
            'tmdb_id': tmdb_id,
            'name': (latest or {}).get('series_name') or sub.get('name'),
            'notified_episodes': keys,
            'missing_episodes': [],
            'latest_ep': max(keys, key=_ep_key_num) if keys else '',
            'updated_at': now_str,
        }
        seeded += 1
    if seeded:
        _save_sub_state(sub_state)
    return {'seeded': seeded, 'errors': errors}


def _run_subscription_check(send_notify=True, self_delivered=False) -> dict:
    """追更订阅检查（集合差集状态机）。

    核心口径（修复「吞通知 / 重复通知 / 跳集不报警」）：

    1. **集合差集**：每次检查取 Emby 实际存在的集 ``have``，与状态文件里
       ``notified_episodes``（已成功通知过的集）求差 → ``new_eps``；与 TMDB 已播集
       求差 → ``missing_eps``。不再只看「最大集号」，所以中间插入的集也能被发现。
    2. **发送成功才记账**：``notify_telegram()`` 返回 True 后才把 ``new_eps`` 并入
       ``notified_episodes``。发送失败则状态原样不动，下一轮自动重试 —— 既不丢通知
       也不重复刷屏。``self_delivered=True``（Bot 回显）则只算不写，由调用方在送达
       成功后 ``commit_sub_check()``；两者都不满足时是只读预览，绝不落盘。
    3. **跳集报警**：``newly_missing``（本次新出现的缺集）与 ``refilled``（本次补齐的
       缺集）分别通知，提示精确到集号区间。
    4. **免迁移**：首次遇到只有旧 ``latest_ep`` 的订阅时，把它当成「已通知过的最后一集」
       向前播种，避免升级后把全库旧集刷一遍。
    5. **取数失败显式化（S0 P1-C）**：Emby 不可达 / Key 未配置抛 ``EmbyError``，
       按订阅收进返回值 ``errors``（sid/name/message），不用「无变化」冒充。
    """
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        return {'updates': [], 'skipped': 'disabled', 'checked': 0, 'errors': []}
    subs = get_subscriptions()
    if not subs:
        return {'updates': [], 'total': 0, 'checked': 0, 'errors': []}

    check_tmdb = cfg.get('subscribe_check_tmdb', '1') == '1'
    state = _load_sub_state()
    updates = []
    errors = []   # [{'sid','name','message'}]：Emby 取数失败显式返回，不冒充「无变化」
    checked = 0   # 本轮成功取到 Emby 数据的订阅数（区分「全部失败」与「无变化」）
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 记录每个订阅本轮「发送成功后应写回」的状态，发送前不落盘
    staged = {}
    # S4：已看进度单轮缓存失效——一轮检查只查一次每个用户的播放记录
    facts.invalidate_watch_cache()

    for sub in subs:
        if not sub.get('enabled', True): continue
        sid = sub.get('id') or sub.get('tmdb_id') or sub.get('name')
        tmdb_id = sub.get('tmdb_id')
        if not tmdb_id: continue

        try:
            latest = _emby_series_latest_ep(tmdb_id)
        except (emby.EmbyError, facts.EmbyFactError) as e:
            errors.append({'sid': sid, 'name': sub.get('name') or tmdb_id, 'message': str(e)})
            continue
        # Emby 的 ProviderIds.Tmdb 缺失（上游 TgtoDrive 目录名有 tmdb 但 Emby 没刮进去）
        # 时，回退到磁盘目录名 tmdb 兜底，避免追更拿不到分集而「不能用」。
        # 注意：Emby 请求本身失败走上面的 EmbyError 分支，磁盘兜底只救「元数据缺失」，
        # 不掩盖「Emby 不可达」。
        if not latest:
            latest = _disk_series_eps(tmdb_id)
        tmdb_info = tmdb._tmdb_series_info(tmdb_id) if check_tmdb else None
        # S4（D4）：已看进度自动读 Emby 播放记录（单轮缓存）；查询失败只降级
        # 为「自动值未知」，不冒充「没看过」，也不让整轮检查失败。
        watch_auto = None
        try:
            watch_auto = (facts.watched_progress(tmdb_id) or {}).get('episode')
        except (facts.EmbyFactError, emby.EmbyError) as e:
            log.warning('已看进度读取失败 %s: %s', tmdb_id, e)
        watched_ep = facts.effective_watched(sub.get('watched_ep_manual'), watch_auto)
        checked += 1

        # 无 Emby 数据且无 TMDB → 跳过（保持旧行为）
        if not latest and not tmdb_info: continue

        prev = state.get(sid) or {}
        have = (latest or {}).get('episodes') or set()

        # ── 免迁移播种 ──
        # 老结构只有 latest_ep：把它当成「已通知过的最后一集」，该季 1..N 全部视为
        # 已通知，避免升级后一次刷出几十条历史集。全新订阅则把当前已有的集全部播种，
        # 首次检查保持安静（避免「刚加订阅就被刷屏」）。
        prev_notified_raw = prev.get('notified_episodes')
        if prev_notified_raw is None:
            prev_latest = prev.get('latest_ep') or ''
            seed = set()
            lp = _parse_ep_key(prev_latest)
            if lp:
                if have:
                    # 只播种 latest_ep 所属季；其它季由后续轮次自然发现
                    seed |= {(sn, en) for (sn, en) in have if sn == lp[0] and en <= lp[1]}
                else:
                    # Emby 查不到分集时保守播种到 latest_ep（假设中间无断层）
                    seed |= {(lp[0], e) for e in range(1, lp[1] + 1)}
            else:
                seed = set(have)          # 全新订阅：当前已有的都算已知
            notified = seed
            migrated = True
        else:
            notified = _keys_to_eps(prev_notified_raw)
            migrated = False

        prev_missing = _keys_to_eps(prev.get('missing_episodes'))

        new_eps = sorted(have - notified)                       # 真实新增
        missing_eps = set()
        if tmdb_info and latest:
            aired = tmdb_info.get('aired') or set()
            # ── 缺集只认「从未入库过的集」──
            # 若只算 aired - have，那么「曾经有、后来被删」的集会被误报成缺集
            # （下架 / 洗版删旧时最容易遇到）。取 aired - (have | notified)：
            # 已通知过的集视为「到过库」，即使当下不在也不算缺集。
            missing_eps = aired - have - notified
        newly_missing = sorted(missing_eps - prev_missing)      # 本次新出现的缺集
        refilled = sorted(set(new_eps) & prev_missing)          # 本次被补齐的缺集

        # 通知只由「状态相对上次的变化」驱动，不看绝对值
        has_change = bool(new_eps or newly_missing or refilled)

        # 新基线：notified_episodes 只在发送成功后才并入 new_eps，所以这里先按
        # 「已通知集 + 本轮新增」算出候选值，发送失败时回退到旧 notified。
        st = {
            'tmdb_id': tmdb_id,
            'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
            'notified_episodes': sorted(_eps_to_keys(notified | set(new_eps))),
            'missing_episodes': sorted(_eps_to_keys(missing_eps)),
            'tmdb_total': (tmdb_info or {}).get('total_episodes', prev.get('tmdb_total') or 0),
            'tmdb_declared': (tmdb_info or {}).get('declared_total', prev.get('tmdb_declared') or 0),
            'tmdb_status': (tmdb_info or {}).get('status', ''),
            'updated_at': now_str,
        }
        # notified_episodes 为空时保持旧 latest_ep，避免 Emby 短暂异常把展示清空
        all_keys = st['notified_episodes']
        st['latest_ep'] = max(all_keys, key=_ep_key_num) if all_keys else (prev.get('latest_ep') or '')
        staged[sid] = st

        if has_change:
            # S4（D4）通知规则：新增集里「超过已看进度」的才算「值得提醒」；
            # 但全部新增仍记录在 new_eps 里不丢（记账不受影响）。
            new_after_watch = (sorted(e for e in new_eps if not watched_ep or e > watched_ep)
                               if new_eps else [])
            updates.append({
                'sid': sid,
                'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
                'tmdb_id': tmdb_id,
                'poster': sub.get('poster', ''),
                'new_eps': sorted(_eps_to_keys(new_eps)),
                'new_after_watch': sorted(_eps_to_keys(new_after_watch)),
                'worth_notifying': bool(new_after_watch or newly_missing or refilled),
                'watched_ep': _ep_key(*watched_ep) if watched_ep else '',
                'watched_source': 'manual' if (sub.get('watched_ep_manual')
                                               and facts.effective_watched(sub.get('watched_ep_manual'), None)) else 'auto',
                'total_eps': (tmdb_info or {}).get('declared_total') or (tmdb_info or {}).get('total_episodes') or 0,
                'aired_total': (tmdb_info or {}).get('total_episodes') or 0,
                'missing_eps': sorted(_eps_to_keys(missing_eps)),
                'newly_missing': sorted(_eps_to_keys(newly_missing)),
                'refilled': sorted(_eps_to_keys(refilled)),
            })

    # ── 发送与记账（两个正交开关）──
    # send_notify=True：推送 Telegram，发送成功才记账；失败时 notified_episodes /
    # missing_episodes 回退到本轮开始前的值，下一轮算出同一批 new_eps 继续重试。
    # self_delivered=True（Bot 回显）：本轮只算不写，staged 随返回值交给调用方，
    # 在「实际送达成功」后由 commit_sub_check() 落盘。
    # 都不满足：只读预览，绝不落盘。
    sent_ok = True
    if send_notify and updates:
        # 文案统一由 tgmsg 渲染（与 Bot /sub check、晨报「订阅更新」同一份实现）
        sent_ok = tg.notify_telegram(str(tgmsg_reports.sub_notify(updates)))
        if sent_ok:
            _save_subscription_report(updates)
        else:
            log.warning('追更订阅推送失败，本轮不记账，下次继续重试')

    if not sent_ok:
        for u in updates:
            st = staged.get(u['sid'])
            if not st:
                continue
            # 回退新增集：把本轮 new_eps 从 notified_episodes 里剔除
            kept = _keys_to_eps(st['notified_episodes']) - _keys_to_eps(u['new_eps'])
            st['notified_episodes'] = sorted(_eps_to_keys(kept))
            st['latest_ep'] = (max(st['notified_episodes'], key=_ep_key_num)
                               if st['notified_episodes'] else '')
            # 缺集基线也不推进：下一轮重新判定 newly_missing
            st['missing_episodes'] = sorted(
                _eps_to_keys(_keys_to_eps(st['missing_episodes']) - _keys_to_eps(u['newly_missing'])))

    staged_out = None
    if self_delivered and not send_notify:
        staged_out = staged            # 送达确认权在调用方（Bot 回显成功后 commit）
    elif send_notify:
        state.update(staged)           # 推送路径：失败已回退，成功照常记账
        _save_sub_state(state)

    out = {'updates': updates, 'total': len(subs), 'checked': checked, 'errors': errors,
           'sent': bool(sent_ok) if send_notify and updates else None}
    if staged_out is not None:
        out['staged'] = staged_out
    return out
