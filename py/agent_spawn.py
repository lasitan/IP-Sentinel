"""后台启动 Agent Python 子任务（统一 cwd、环境变量与 spawn.log）."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from config import DEFAULT_INSTALL_DIR, load_config
from log_util import log
from task_lock import browser_busy, is_browser_script, is_trust_script, trust_busy

_PY_DIR = Path(__file__).resolve().parent


def resolve_py_script(script: str, cfg: dict | None = None) -> Path | None:
    cfg = cfg or load_config()
    if not cfg:
        install = Path(os.environ.get("IP_SENTINEL_INSTALL_DIR", DEFAULT_INSTALL_DIR))
    else:
        install = Path(cfg.get("INSTALL_DIR", DEFAULT_INSTALL_DIR))
    for candidate in (install / "py" / script, _PY_DIR / script):
        if candidate.is_file():
            return candidate
    return None


def spawn_py_script(
    script: str,
    *,
    log_module: str = "SYSTEM",
    nice: bool = False,
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
    }
    rel = f"py/{script}"
    uv_bin = shutil.which("uv") or "/usr/local/bin/uv"
    proj = Path(install) / "pyproject.toml"
    if uv_bin and proj.is_file():
        cmd = [uv_bin, "run", "--directory", install, "python", rel]
    else:
        cmd = [sys.executable, str(path)]
    if nice:
        cmd = ["nice", "-n", "19", *cmd]
    with open(spawn_log, "a", encoding="utf-8") as logf:
        subprocess.Popen(
            cmd,
            cwd=install,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    log(cfg, log_module, "INFO ", f"已后台启动: {script}")
    return True
