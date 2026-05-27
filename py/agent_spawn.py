"""后台启动 Agent Python 子任务（统一 cwd、环境变量与 spawn.log）."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config import DEFAULT_INSTALL_DIR, load_config
from log_util import log

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
    install = cfg.get("INSTALL_DIR", DEFAULT_INSTALL_DIR)
    spawn_log = Path(install) / "logs" / "spawn.log"
    spawn_log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "IP_SENTINEL_INSTALL_DIR": install}
    cmd: list[str] = [sys.executable, str(path)]
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
