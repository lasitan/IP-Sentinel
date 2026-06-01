"""后台启动 Agent Python 子任务（统一 cwd、环境变量与 spawn.log）."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import DEFAULT_INSTALL_DIR, load_config
from log_util import log
from task_lock import browser_busy, is_browser_script, is_trust_script, trust_busy

_PY_DIR = Path(__file__).resolve().parent
_SPAWN_GRACE_SEC = 0.4


def resolve_py_script(script: str, cfg: dict | None = None) -> Path | None:
    cfg = cfg or load_config()
    if not cfg:
        install = Path(os.environ.get("IP_SENTINEL_INSTALL_DIR", DEFAULT_INSTALL_DIR))
    else:
        install = Path(cfg.get("INSTALL_DIR", DEFAULT_INSTALL_DIR))
    for candidate in (install / "py" / script, _PY_DIR / script):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _python_for_install(install: str) -> str | None:
    """优先使用项目 .venv，避免 runner/webhook 内再嵌套 uv run."""
    venv_py = Path(install) / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    if sys.executable and Path(sys.executable).is_file():
        return sys.executable
    return None


def _build_spawn_cmd(path: Path, install: str) -> list[str]:
    script = str(path)
    py = _python_for_install(install)
    if py:
        return [py, script]
    uv_bin = shutil.which("uv") or "/usr/local/bin/uv"
    if uv_bin and (Path(install) / "pyproject.toml").is_file():
        return [uv_bin, "run", "--directory", install, "python", script]
    return [sys.executable, script]


def _lower_nice() -> None:
    try:
        os.nice(19)
    except OSError:
        pass


def spawn_py_script(
    script: str,
    *,
    log_module: str = "SYSTEM",
    nice: bool = False,
    extra_env: dict[str, str] | None = None,
) -> bool:
    cfg = load_config()
    if not cfg:
        return False
    path = resolve_py_script(script, cfg)
    if not path:
        log(cfg, log_module, "ERROR", f"未找到脚本 {script}，无法启动任务")
        return False

    if is_browser_script(script):
        busy, holder = browser_busy()
        if busy:
            log(
                cfg,
                log_module,
                "WARN ",
                f"Google 纠偏进行中 (pid={holder})，拒绝重复启动。",
            )
            return False
    if is_trust_script(script):
        busy, holder = trust_busy()
        if busy:
            log(
                cfg,
                log_module,
                "WARN ",
                f"信用净化进行中 (pid={holder})，拒绝重复启动。",
            )
            return False

    install = cfg.get("INSTALL_DIR", DEFAULT_INSTALL_DIR)
    spawn_log = Path(install) / "logs" / "spawn.log"
    spawn_log.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "IP_SENTINEL_INSTALL_DIR": install,
        "IP_SENTINEL_CONFIG": f"{install.rstrip('/')}/config.conf",
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        env.update(extra_env)

    cmd = _build_spawn_cmd(path, install)
    preexec = _lower_nice if nice and platform.system() != "Windows" else None
    if nice and platform.system() == "Windows":
        pass  # Windows 无 os.nice，直接启动

    try:
        with open(spawn_log, "a", encoding="utf-8") as logf:
            logf.write(f"\n--- spawn {script} cmd={' '.join(cmd)} ---\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=install,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                preexec_fn=preexec,
            )
    except OSError as exc:
        log(cfg, log_module, "ERROR", f"子进程启动异常 ({script}): {exc}")
        return False

    time.sleep(_SPAWN_GRACE_SEC)
    exit_code = proc.poll()
    if exit_code is not None:
        log(
            cfg,
            log_module,
            "ERROR",
            f"子进程立即退出 code={exit_code} ({script})，详见 logs/spawn.log",
        )
        return False

    log(cfg, log_module, "INFO ", f"已后台启动: {script} (pid={proc.pid})")
    return True
