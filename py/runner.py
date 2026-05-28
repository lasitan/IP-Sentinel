#!/usr/bin/env python3
"""主控调度：排他锁、Cron 抖动、模块轮盘."""

from __future__ import annotations

import fcntl
import os
import random
import sys
import time

from agent_spawn import resolve_py_script, spawn_py_script
from config import default_install_dir, require_config
from log_util import log

LOCK_PATH = "/tmp/ip_sentinel_runner.lock"


def _bootstrap_log(message: str) -> None:
    """cron 在 require_config 失败等场景仍写入日志."""
    try:
        install = default_install_dir()
        log_path = os.path.join(install, "logs", "sentinel.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [SYSTEM ] [ERROR] {message}\n")
    except OSError:
        pass


def _acquire_lock(cfg: dict) -> int | None:
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        log_file = cfg.get("LOG_FILE", "")
        msg = f"[{time.strftime('%c')}] ⚠️ 上一轮巡逻任务尚未结束，本次触发自动取消。\n"
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(msg)
        return None


def _pick_module(cfg: dict) -> tuple[str, str] | None:
    g = cfg.get("ENABLE_GOOGLE", "false").lower() == "true"
    t = cfg.get("ENABLE_TRUST", "false").lower() == "true"
    if g and t:
        if random.randint(1, 100) <= 70:
            return "mod_google.py", "Google 区域纠偏"
        return "mod_trust.py", "IP 信用净化"
    if g:
        return "mod_google.py", "Google 区域纠偏"
    if t:
        return "mod_trust.py", "IP 信用净化"
    return None


def run() -> int:
    try:
        cfg = require_config()
    except SystemExit as exc:
        _bootstrap_log(f"runner 启动失败: {exc}")
        return 1

    log(cfg, "SYSTEM", "INFO ", "主控 runner 已唤醒（定时或手动触发）")
    lock_fd = _acquire_lock(cfg)
    if lock_fd is None:
        return 0

    try:
        if sys.stdout.isatty():
            log(cfg, "SYSTEM", "INFO ", "💻 检测到人工终端干预，跳过静默休眠，立即执行任务！")
        else:
            jitter = random.randint(0, 179)
            log(cfg, "SYSTEM", "INFO ", f"⏱️ 主控引擎由后台唤醒，进入防并发随机休眠状态: {jitter} 秒...")
            time.sleep(jitter)

        log(cfg, "SYSTEM", "INFO", "休眠结束，开始计算本轮任务轮盘...")
        picked = _pick_module(cfg)
        if not picked:
            log(cfg, "SYSTEM", "WARN", "未启用任何维护模块，跳过本轮。")
            return 0

        script_name, mod_name = picked
        if not resolve_py_script(script_name, cfg):
            log(cfg, "SYSTEM", "ERROR", f"配置了模块 {mod_name}，但未找到: {script_name}")
            return 1

        log(cfg, "SYSTEM", "INFO", f"命中触发条件，加载并执行子模块: {mod_name}")
        if not spawn_py_script(script_name, log_module="SYSTEM", nice=True):
            log(cfg, "SYSTEM", "ERROR", f"子模块启动失败: {script_name}")
            return 1
        log(cfg, "SYSTEM", "INFO", "本轮模块调度完成。")
        return 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
