# -*- coding: utf-8 -*-
"""测试公共环境：在任何 app 模块导入之前，把媒体根目录 / 数据目录指到同一个会话级临时目录。

- 只有一份模块副本：一律 `from app import ...`，不再把 app/ 塞进 sys.path 做顶层导入；
- 环境变量用 setdefault：外部显式指定时以外部为准（例如在容器里指定 AGENT_DATA 排查问题）；
- isolated_state 夹具：把 state.STATE_DIR（SQLite 库目录）改到 tmp_path，测试结束关闭连接并自动还原。
"""
import errno
import os
import sys
import tempfile
from pathlib import Path

# ── Windows 兼容垫片：fcntl → msvcrt ─────────────────────────────
# app/ 里 8 个模块第 3 行顶层 `import fcntl`（wash.mutation_lock 用 flock 做
# 「跨入口治理互斥」），tests 里另有 3 个文件直接 import fcntl 持锁构造「忙碌」
# 场景；Windows 没有该模块，不垫片本地连 pytest 收集都过不了。垫片以真实模块文件
# 的形式生成到会话临时目录：本进程挂到 sys.path[0]，test_main / test_engine 用
# `python -c` 起的子进程解释器经 PYTHONPATH 继承，两边行为完全一致。
# CI（ubuntu）上真 fcntl 可导入时什么都不做。
# 语义映射：本仓库只用 flock 做「整文件独占」互斥，等价于锁 0 号字节：
#   flock(fd, LOCK_EX|LOCK_NB) → msvcrt.locking(fd, LK_NBLCK, 1)，被占用抛 BlockingIOError
#   flock(fd, LOCK_EX)         → msvcrt.locking(fd, LK_LOCK, 1)（msvcrt 内部重试约 10 秒）
#   flock(fd, LOCK_UN)         → msvcrt.locking(fd, LK_UNLCK, 1)，未持锁时静默忽略
# Windows 的区域锁跨句柄冲突（含同进程两个句柄，已实测），与 POSIX flock 的
# 「同进程两个 fd 也互斥」一致，test_safety / test_main / test_engine 的持锁用例语义不变。
_FCNTL_SHIM_SRC = '''"""Windows fcntl 兼容垫片（由 tests/conftest.py 生成到会话临时目录）。

仅实现本仓库用到的 flock(fd, op)：整文件独占锁映射到 msvcrt.locking 的 0 号字节
区域锁；被占用时抛 BlockingIOError（wash.MutationBusy 依赖这一异常类型）。
"""
import errno
import os

try:
    import msvcrt
except ImportError:            # 非 Windows 误把垫片目录挂上了 sys.path
    raise ImportError('fcntl shim 仅用于 Windows')

LOCK_SH = 0x01
LOCK_EX = 0x02
LOCK_NB = 0x04
LOCK_UN = 0x08


def _fileno(fd):
    return fd.fileno() if hasattr(fd, 'fileno') else int(fd)


def flock(fd, op):
    nfd = _fileno(fd)
    pos = os.lseek(nfd, 0, os.SEEK_CUR)    # msvcrt.locking 相对当前位置操作，锁完恢复
    try:
        if op & LOCK_UN:
            os.lseek(nfd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(nfd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass                        # POSIX 对未持锁的 LOCK_UN 是无害操作
            return
        os.lseek(nfd, 0, os.SEEK_SET)
        mode = msvcrt.LK_NBLCK if (op & LOCK_NB) else msvcrt.LK_LOCK
        msvcrt.locking(nfd, mode, 1)
    except OSError as e:
        raise BlockingIOError(errno.EAGAIN, f'fcntl 垫片：文件已被锁定（{e}）')
    finally:
        os.lseek(nfd, pos, os.SEEK_SET)
'''

try:
    import fcntl  # noqa: F401
except ImportError:
    _FCNTL_DIR = Path(tempfile.mkdtemp(prefix='ttdguard_fcntl_shim_'))
    (_FCNTL_DIR / 'fcntl.py').write_text(_FCNTL_SHIM_SRC, encoding='utf-8')
    sys.path.insert(0, str(_FCNTL_DIR))
    import fcntl  # noqa: F401  # noqa: E402  # 命中刚写入的垫片
    # 子进程（python -c 独立解释器）也要能 import fcntl
    _pp = os.environ.get('PYTHONPATH')
    os.environ['PYTHONPATH'] = str(_FCNTL_DIR) + (os.pathsep + _pp if _pp else '')

try:
    import pytest
except ImportError:          # tests/run_engine_tests.py 的无 pytest 场景：只需要上面的环境变量
    pytest = None

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SESSION_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_'))
os.environ.setdefault('L_ROOT', str(_SESSION_TMP / 'local'))
os.environ.setdefault('S_ROOT', str(_SESSION_TMP / 'share'))
os.environ.setdefault('CLOUD_L_ROOT', str(_SESSION_TMP / 'cloud'))
os.environ.setdefault('AGENT_DATA', str(_SESSION_TMP / 'data'))
os.environ.setdefault('TMDB_KEY', '')
os.environ.setdefault('TG_BOT_TOKEN', '')
os.environ.setdefault('TG_ALLOWED_USERS', '')  # 白名单从 config.json 读，不从这个环境变量的默认值读
# 测试里的 STRM 都是刚创建的，先关掉「入库静默期」，静默期本身在 TestIngestQuietPeriod 里单独测
os.environ.setdefault('INGEST_QUIET_MINUTES', '0')


def isolated_state(tmp_path, monkeypatch):
    """把 state.STATE_DIR（SQLite 库所在目录）改到 tmp_path/state，每个测试一份独立的库；
    结束时 storage.close_all() 释放连接。产出新的 STATE_DIR。"""
    from app import state, storage
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    monkeypatch.setattr(state, 'LOCK_FILE', tmp_path / state.LOCK_FILE.name)
    try:
        yield sd
    finally:
        storage.close_all()


if pytest is not None:
    isolated_state = pytest.fixture(isolated_state)

    @pytest.fixture(autouse=True)
    def _reset_media_cache():
        """app/media.py 有进程级 60 秒库清单缓存：每个测试前后清一次，避免一个测试
        monkeypatch 的假 Emby 数据泄漏到下一个测试（与 emby._ep_cache 不同，它跨测试保留）。"""
        from app import media
        media.invalidate()
        yield
        media.invalidate()
