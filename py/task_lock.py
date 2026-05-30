"""维护任务锁：Playwright 纠偏与信用净化分锁，质量检测不占用锁."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

_BROWSER_LOCK_PATH = Path("/tmp/ip_sentinel_browser.lock")
_TRUST_LOCK_PATH = Path("/tmp/ip_sentinel_trust.lock")

_BROWSER_SCRIPTS = frozenset({"mod_google.py"})
_TRUST_SCRIPTS = frozenset({"mod_trust.py"})


def is_browser_script(script: str) -> bool:
    return script in _BROWSER_SCRIPTS


def is_trust_script(script: str) -> bool:
    return script in _TRUST_SCRIPTS


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_pid(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _busy(path: Path) -> tuple[bool, int | None]:
    pid = _read_lock_pid(path)
    if pid is None:
        return False, None
    if _pid_alive(pid):
        return True, pid
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False, None


def browser_busy() -> tuple[bool, int | None]:
    return _busy(_BROWSER_LOCK_PATH)


def trust_busy() -> tuple[bool, int | None]:
    return _busy(_TRUST_LOCK_PATH)


def maintenance_busy() -> tuple[bool, int | None]:
    """runner 调度：浏览器纠偏或净化任一在跑则跳过."""
    b, bp = browser_busy()
    if b:
        return True, bp
    t, tp = trust_busy()
    if t:
        return True, tp
    return False, None


def _acquire(path: Path) -> bool:
    busy, _ = _busy(path)
    if busy:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    return fd


def _release(path: Path, fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def acquire_browser_lock() -> bool:
    fd = _acquire(_BROWSER_LOCK_PATH)
    if fd is False:
        return False
    acquire_browser_lock._fd = fd  # type: ignore[attr-defined]
    return True


def release_browser_lock() -> None:
    fd = getattr(acquire_browser_lock, "_fd", None)
    _release(_BROWSER_LOCK_PATH, fd)
    acquire_browser_lock._fd = None  # type: ignore[attr-defined]


def acquire_trust_lock() -> bool:
    fd = _acquire(_TRUST_LOCK_PATH)
    if fd is False:
        return False
    acquire_trust_lock._fd = fd  # type: ignore[attr-defined]
    return True


def release_trust_lock() -> None:
    fd = getattr(acquire_trust_lock, "_fd", None)
    _release(_TRUST_LOCK_PATH, fd)
    acquire_trust_lock._fd = None  # type: ignore[attr-defined]


# 兼容旧名
def acquire_maintenance_lock() -> bool:
    return acquire_browser_lock()


def release_maintenance_lock() -> None:
    release_browser_lock()
