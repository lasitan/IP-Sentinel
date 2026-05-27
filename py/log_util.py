"""统一 UTC 日志与 systemd journal 桥接."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any


def _ensure_log_dir(cfg: dict[str, Any]) -> None:
    log_file = cfg.get("LOG_FILE", "")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)


def format_core_msg(cfg: dict[str, Any], module: str, level: str, message: str) -> str:
    ver = cfg.get("AGENT_VERSION", "未知")
    region = cfg.get("REGION_CODE", "SYSTEM")
    return f"[v{ver:<5}] [{level:<5}] [{module:<7}] [{region}] {message}"


def log(cfg: dict[str, Any], module: str, level: str, message: str) -> None:
    _ensure_log_dir(cfg)
    core_msg = format_core_msg(cfg, module, level, message)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {core_msg}\n"
    log_file = cfg.get("LOG_FILE", "")
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    if shutil.which("logger"):
        subprocess.run(["logger", "-t", "ip-sentinel", core_msg], check=False)
    else:
        print(core_msg, flush=True)


def log_trust(cfg: dict[str, Any], level: str, message: str) -> None:
    """Trust 模块日志格式 (模块名固定为 Trust)."""
    _ensure_log_dir(cfg)
    ver = cfg.get("AGENT_VERSION", "未知")
    region = cfg.get("REGION_CODE", "US")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [v{ver:<5}] [{level:<5}] [Trust  ] [{region}] {message}\n"
    log_file = cfg.get("LOG_FILE", "")
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
        print(line.rstrip())
