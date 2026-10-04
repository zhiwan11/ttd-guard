# -*- coding: utf-8 -*-
"""追更订阅 / 每日晨报（v1.7.7 自 main.py 拆出）"""
import time
from fastapi import APIRouter, Depends, HTTPException

from app import config as _cfg
from app import state, morning, subscribe, sync, media, emby
from app.routers.deps import auth

router = APIRouter()


@router.get('/api/subscriptions', dependencies=[Depends(auth)])
def api_get_subs():
    subs = subscribe.get_subscriptions()
    sub_state = subscribe.load_sub_state()
    out = []
    for s in subs:
        sid = s.get('id') or s.get('tmdb_id') or s.get('name')
        info = dict(s)
        st = sub_state.get(sid) or {}
        # 记账值（「已通知到 SxxExx」）单独一行小字展示，不再冒充「最新集」
        info['notified_ep'] = st.get('latest_ep', '')
        info['updated_at'] = st.get('updated_at', '')
        info['tmdb_total'] = st.get('tmdb_total', 0)
        info['tmdb_declared'] = st.get('tmdb_declared', 0)
        info['tmdb_status'] = st.get('tmdb_status', '')
        # P1-B：卡片「最新集」来自 Emby 实时现状 + 数据时间；取数失败显式标记
        # （data_error），不用记账值冒充现状。S1 事实层建成后切换 facts.get_series_status。
        # 查不到分集不算失败：未入库是诚实的现状（前端显示「未入库」），
        # 只有 EmbyError（挂了 / Key 未配置 / 请求失败）才是白皮书 §5.4 修复3 的告警。
        tmdb_id = s.get('tmdb_id')
        if tmdb_id:
            try:
                live = media.series_live(tmdb_id, use_cache=True)
                if live and live.get('episodes'):
                    sn, en = max(live['episodes'])
                    info['live_ep'] = f'S{int(sn):02d}E{int(en):02d}'
                    info['live_eps'] = len(live['episodes'])
                    info['data_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime())
                else:
                    info['live_ep'] = ''
            except emby.EmbyError as e:
                info['live_ep'] = ''
                info['data_error'] = str(e)
        out.append(info)
    cfg = _cfg.load_config()
    return {'status': 'success', 'subscriptions': out,
            'enabled': cfg.get('subscribe_enabled', '1') == '1',
            'interval_min': int(cfg.get('subscribe_interval_min') or 30),
            'check_tmdb': cfg.get('subscribe_check_tmdb', '1') == '1'}


@router.post('/api/subscriptions', dependencies=[Depends(auth)])
def api_save_subs(body: dict = None):
    body = body or {}
    subs = body.get('subscriptions') or []
    if not isinstance(subs, list):
        raise HTTPException(400, 'subscriptions 必须是数组')
    clean = []
    for s in subs:
        if not isinstance(s, dict): continue
        name = str(s.get('name') or '').strip()
        if not name: continue
        tmdb_id = s.get('tmdb_id')
        clean.append({
            'id': str(s.get('id') or tmdb_id or name),
            'name': name,
            'tmdb_id': str(tmdb_id) if tmdb_id else '',
            'poster': str(s.get('poster') or ''),
            'enabled': bool(s.get('enabled', True)),
            'added_at': s.get('added_at') or time.strftime('%Y-%m-%d %H:%M:%S'),
        })
    known = {s.get('id') for s in subscribe.get_subscriptions()}
    subscribe.set_subscriptions(clean)
    # P1-A（白皮书 §5.4 修复2）：保存订阅不再触发整轮检查 —— 旧实现保存后跑
    # check_subscriptions(send_notify=False)，既拖慢保存，又把「预览」当「已送达」记账。
    # 现在只对「新增订阅」播种一次基线（把当前已有集记为 notified_episodes），防刷屏；
    # 已有订阅的状态一律不动。播种失败必须诚实提示：此时下一轮检查的状态机会把它
    # 当全新订阅安静播种，不会刷屏，但基线时间点会晚于订阅时刻。
    warning = ''
    new_subs = [s for s in clean if s.get('id') not in known and s.get('tmdb_id')]
    if new_subs:
        try:
            r = subscribe.seed_new_subscriptions(new_subs)
            if r.get('errors'):
                warning = '订阅已保存，但建立基线失败：%s' % (r['errors'][0].get('message') or 'Emby 查询失败')
        except Exception as e:
            warning = '订阅已保存，但建立基线失败：%s' % e
    sync.bump('subscriptions', reason='subs_save')
    out = {'status': 'success', 'subscriptions': subscribe.get_subscriptions()}
    if warning:
        out['warning'] = warning
    return out


@router.post('/api/subscriptions/settings', dependencies=[Depends(auth)])
def api_subs_settings(body: dict = None):
    body = body or {}

    def _apply(cfg):
        if 'enabled' in body:
            cfg['subscribe_enabled'] = '1' if body['enabled'] else '0'
        if 'interval_min' in body:
            try:
                v = max(5, min(1440, int(body['interval_min'])))
            except (ValueError, TypeError):
                v = 30
            cfg['subscribe_interval_min'] = str(v)
        if 'check_tmdb' in body:
            cfg['subscribe_check_tmdb'] = '1' if body['check_tmdb'] else '0'
    cfg = _cfg.update_config(_apply)
    state.reload_config()
    sync.bump('subscriptions', reason='subs_settings')
    return {'status': 'success',
            'enabled': cfg['subscribe_enabled'] == '1',
            'interval_min': int(cfg['subscribe_interval_min']),
            'check_tmdb': cfg['subscribe_check_tmdb'] == '1'}


@router.post('/api/subscriptions/check', dependencies=[Depends(auth)])
def api_subs_check_now():
    try:
        # 手动「立即检查」与定时检查同口径：推送 Telegram；推送成功才记账，
        # 之后定时检查不会重复推同一批集（P1-A「发送成功才记账」）。
        r = subscribe.check_subscriptions(send_notify=True)
    except Exception as e:
        return {'status': 'error', 'ok': False, 'reason': str(e)}
    sync.bump('subscriptions', reason='subs_check')
    errs = r.get('errors') or []
    # P1-C：一部都没查成（Emby 断开 / Key 未配置 / 超时）→ 显式报错，
    # 不能当成「无变化」；部分失败仍算成功，错误明细随响应带给前端卡片告警。
    if errs and not r.get('checked'):
        return {'status': 'error', 'ok': False,
                'reason': errs[0].get('message') or 'Emby 查询失败', 'errors': errs}
    return {'status': 'success', 'ok': True, **r}


@router.get('/api/morning', dependencies=[Depends(auth)])
def api_get_morning():
    return {'status': 'success', 'morning': _cfg.get_morning_report(), 'tz': state.tz_info()}


@router.post('/api/morning', dependencies=[Depends(auth)])
def api_set_morning(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'enabled' in body: kwargs['enabled'] = bool(body['enabled'])
    if 'hour' in body:    kwargs['hour'] = int(body['hour'])
    if 'minute' in body:  kwargs['minute'] = int(body['minute'])
    if 'items' in body:   kwargs['items'] = body['items']
    if 'prescan_min' in body: kwargs['prescan_min'] = int(body['prescan_min'])
    result = _cfg.update_morning_report(**kwargs)
    return {'status': 'success', 'morning': result}


@router.post('/api/morning/preview', dependencies=[Depends(auth)])
def api_preview_morning(body: dict = None):
    body = body or {}
    mr = _cfg.get_morning_report()
    items = body.get('items') or mr.get('items') or []
    force = bool(body.get('force', False))
    try:
        # 缓存预览严格只读已有缓存；现场扫严格重建，二者不再互相兜底。
        text = morning.build_morning_report(items, force_refresh=force, cache_only=not force)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    import re as _re, html as _html
    text = _html.unescape(_re.sub(r'</?(?:b|i|code|pre|blockquote)[^>]*>', '', text))  # 网页预览按纯文本显示，去掉 Telegram 标记
    return {'status': 'success', 'text': text, 'force': force}


@router.post('/api/morning/send', dependencies=[Depends(auth)])
def api_send_morning(body: dict = None):
    body = body or {}
    mr = _cfg.get_morning_report()
    items = body.get('items') or mr.get('items') or []
    # 立即发送与定时晨报保持同一口径：发送当前晨报缓存，不在 HTTP 请求里触发现场扫描。
    # 需要现场扫描请使用「预览（现场扫）」；API 调用方仍可显式传 force=true。
    force = bool(body.get('force', False))
    try:
        ok = morning.send_morning_report(items, force_refresh=force, mark_sent=False)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success' if ok else 'error',
            'message': '✅ 已发送到 Telegram' if ok else '❌ 发送失败（检查 Telegram 配置）',
            'force': force}
