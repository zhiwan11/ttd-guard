"""测试兼容层：生产代码已在 S7 移除 engine.py 的业务重导出；旧回归测试仍用 engine 命名空间。
这里仅在测试进程中提供可写代理，不进入生产包。
"""
import sys, os

# ═══ Windows 兼容 shim（仅测试进程树）═══
# app/ 各模块顶层 import fcntl，Windows 上没有该模块，pytest 会直接收集失败。
# 必须在导入 app 之前用 msvcrt 实现等价的区段锁并注册为 fcntl 替身模块；
# Linux/CI 上 fcntl 可正常导入，此段零影响。不改动 app/ 的 fcntl 用法。
# 部分测试还会用 subprocess 起 `python -c` 导入 app，子进程不执行本 conftest，
# 因此同一份替身源码也落成临时 fcntl.py 挂到 PYTHONPATH，仅测试进程树可见。
_FCNTL_STUB_SRC = '''\
"""Windows 测试替身：由 tests/conftest.py 生成/注册，用 msvcrt 模拟 fcntl 区段锁。"""
import msvcrt


def flock(fd, operation):
    """锁定当前文件位置起 1 字节；非阻塞抢锁失败抛 BlockingIOError，
    与 wash.mutation_lock 的 except 语义对齐（msvcrt 失败抛 PermissionError，这里归一化）。"""
    handle = fd.fileno() if hasattr(fd, 'fileno') else fd
    if operation & 8:  # LOCK_UN
        try:
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # 已释放/未持锁时静默，与 Linux flock 的宽松行为一致
        return
    mode = msvcrt.LK_NBLCK if operation & 4 else msvcrt.LK_LOCK  # LOCK_NB=4
    try:
        msvcrt.locking(handle, mode, 1)
    except OSError as e:
        raise BlockingIOError(e.errno, '锁被占用（msvcrt 区段锁）')


LOCK_SH = 1  # 常量值与 Linux fcntl 模块一致；共享锁退化为独占（测试未用到）
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8
'''
try:
    import fcntl  # noqa: F401  POSIX：真模块，无需任何处理
except ImportError:  # pragma: no cover —— 仅 Windows 走到这里
    import tempfile
    from types import ModuleType as _ModType

    _fcntl = _ModType('fcntl')
    exec(compile(_FCNTL_STUB_SRC, '<fcntl_stub>', 'exec'), _fcntl.__dict__)
    sys.modules['fcntl'] = _fcntl

    import textwrap as _textwrap
    from pathlib import Path as _Path
    _stub_dir = _Path(tempfile.mkdtemp(prefix='ttdguard_fcntl_shim_'))
    (_stub_dir / 'fcntl.py').write_text(_textwrap.dedent(_FCNTL_STUB_SRC), encoding='utf-8')
    os.environ['PYTHONPATH'] = str(_stub_dir) + os.pathsep + os.environ.get('PYTHONPATH', '')

