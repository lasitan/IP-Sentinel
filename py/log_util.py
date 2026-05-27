"""统一 UTC 日志与 systemd journal 桥接."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]")
LOG_RETENTION_DAYS = 2


def parse_log_line_ts(line: str) -> datetime | None:
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def lines_within_hours(lines: list[str], hours: float) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [ln for ln in lines if (ts := parse_log_line_ts(ln)) is not None and ts >= cutoff]


def load_log_lines_within_hours(log_path: Path, hours: float = 48.0) -> list[str]:
    """从日志尾部向前扫描，返回时间窗口内的行。"""
    if not log_path.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    kept: list[str] = []
    for line in reversed(lines):
        ts = parse_log_line_ts(line)
        if ts is None:
            continue
        if ts < cutoff:
            break
        kept.append(line)
    kept.reverse()
    return kept


def prune_log_file(log_path: Path, keep_days: float = LOG_RETENTION_DAYS) -> int:
    """删除早于 keep_days 的日志行，返回删除的行数。"""
    if not log_path.is_file():
        return 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    kept = lines_within_hours(lines, keep_days * 24)
    removed = len(lines) - len(kept)
    if removed > 0:
        log_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


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
