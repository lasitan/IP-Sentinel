"""长耗时维护任务互斥锁（Google 纠偏 / 信用净化 / 质量检测）."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

_LOCK_PATH = Path("/tmp/ip_sentinel_maintenance.lock")

_MAINTENANCE_SCRIPTS = frozenset(
    {
        "mod_google.py",
        "mod_trust.py",
        "mod_quality.py",
    }
)


def is_maintenance_script(script: str) -> bool:
    return script in _MAINTENANCE_SCRIPTS


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_pid() -> int | None:
    if not _LOCK_PATH.is_file():
        return None
    try:
        raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def maintenance_busy() -> tuple[bool, int | None]:
    """返回 (是否繁忙, 持有锁的 pid). 陈旧锁文件会自动清理."""
    pid = _read_lock_pid()
    if pid is None:
        return False, None
    if _pid_alive(pid):
        return True, pid
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return False, None


def acquire_maintenance_lock() -> bool:
    """在维护脚本入口调用；成功则当前进程持有锁直至 release."""
    busy, pid = maintenance_busy()
    if busy:
        return False

    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False

    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    # 保持 fd 打开以维持 flock，进程退出时内核自动释放
    acquire_maintenance_lock._held_fd = fd  # type: ignore[attr-defined]
    return True


def release_maintenance_lock() -> None:
    fd = getattr(acquire_maintenance_lock, "_held_fd", None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    acquire_maintenance_lock._held_fd = None  # type: ignore[attr-defined]
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
