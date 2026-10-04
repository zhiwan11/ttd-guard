# -*- coding: utf-8 -*-
"""路由共享依赖（v1.7.7 自 main.py 原样拆出）：鉴权、会话、任务派发、公共常量。

所有 APIRouter 共用同一个 auth/spawn/会话签名实现，保证拆路由前后行为一致。
WEB_PASSWORD 的启动检查保留在本模块导入时执行（与旧 main.py 的导入时序一致：
state/bot 等业务模块先导入，密码检查后执行），子进程测试依赖这一行为。
"""
import os, secrets, logging
from pathlib import Path
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import state, bot, logger, scheduler, tasks  # noqa: F401
from app import security as _sec


WEB_USER = (os.environ.get('WEB_USER', '').strip() or 'admin')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '').strip()
ALLOW_NO_AUTH = os.environ.get('ALLOW_NO_AUTH', '').strip() == '1'

if not WEB_PASSWORD:
    # S7（§5.9 / D5 默认关）：空密码不再拒绝启动——是否免登录由设置页
    # 「免登录（仅局域网）」开关（config: web_no_auth，默认 0）显式决定；
    # 这里只提示一次。auth() 的实际放行看 web_no_auth。
    logging.getLogger('media_agent').warning(
        '未设置 WEB_PASSWORD：Web 管理台是否免登录由设置页「免登录（仅局域网）」开关'
        '（web_no_auth，默认关）决定。公网域名下开启免登录等于任何知道网址的人都能执行清理。'
    )

# 版本号由 Docker 构建时按 git tag 注入（APP_VERSION），本地直接运行显示 dev
APP_VERSION = os.environ.get('APP_VERSION', 'dev')


def _no_auth_mode() -> bool:
    """免登录是否生效（D5 默认关）：web_no_auth=1（设置页开关）或环境变量
    ALLOW_NO_AUTH=1（旧隐式路径，保留兼容）且 WEB_PASSWORD 为空。"""
    if WEB_PASSWORD:
        return False
    if ALLOW_NO_AUTH:
        return True
    try:
        from app import config as _cfg
        return (_cfg.load_config().get('web_no_auth') or '0') == '1'
    except Exception:
        return False


_security = HTTPBasic(auto_error=False)

# ── Cookie 会话登录 ──
# 原来只有浏览器原生 Basic 弹框，手机 Safari 经常不记住，每次都要重输。
# 现在登录一次后写入 30 天有效的签名 Cookie；Basic 头依然兼容（脚本/curl 可继续用）。
SESSION_COOKIE = 'tdd_session'
SESSION_DAYS = int(os.environ.get('SESSION_DAYS', '30') or 30)
_SESSION_KEY = (os.environ.get('SESSION_SECRET', '').strip() or WEB_PASSWORD or 'noauth').encode()


def _sign_session(user: str, exp: int) -> str:
    return _sec.sign_session(_SESSION_KEY, user, exp)


def _verify_session(token: str) -> bool:
    return _sec.verify_session(_SESSION_KEY, WEB_USER, token)


# ── CSRF + 安全响应头（中间件在 main.py 注册，白名单在这里解析）──
# 反向代理改写了 Host 且没传 X-Forwarded-Host 时，用 ALLOWED_ORIGINS 补白名单（逗号分隔）。
_ALLOWED_HOSTS = _sec.parse_allowed_origins(os.environ.get('ALLOWED_ORIGINS', ''))


def auth(request: Request = None, credentials: HTTPBasicCredentials = Depends(_security)):
    # 保持对旧式单元测试/内部调用的兼容：auth(credentials)。
    if isinstance(request, HTTPBasicCredentials) and not isinstance(credentials, HTTPBasicCredentials):
        credentials, request = request, None
    if not isinstance(credentials, HTTPBasicCredentials):
        credentials = None
    if _no_auth_mode():
        return True  # 免登录（web_no_auth=1 或 ALLOW_NO_AUTH=1，且 WEB_PASSWORD 为空）
    tok = request.cookies.get(SESSION_COOKIE) if request is not None else None
    if tok and _verify_session(tok):
        return True
    ok = bool(credentials) and \
        secrets.compare_digest(credentials.username, WEB_USER) and \
        secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not ok:
        # 故意不返回 WWW-Authenticate：否则浏览器会弹原生登录框，前端改用自带登录页
        raise HTTPException(status_code=401, detail='未登录或登录已过期')
    return True


# ═══════════════════ 任务系统 ═══════════════════
# 任务登记与互斥在 app/tasks.py，Web / Bot / 定时巡检共用同一个 manager
class Args:
    def __init__(self, kw='', plan='', dry_run=False, **kwargs):
        self.kw = kw; self.plan = plan; self.dry_run = dry_run
        for k, v in kwargs.items(): setattr(self, k, v)


def spawn(kind, fn, *fargs):
    try:
        return tasks.manager.spawn(kind, fn, *fargs, source='web')
    except tasks.TaskBusy as e:
        raise HTTPException(429, str(e))


def _current_task_dict():
    cur = tasks.manager.running()
    return cur.to_dict(with_logs=False) if cur else None


STATIC_DIR = Path(__file__).parent.parent.parent / 'static'
