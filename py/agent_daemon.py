#!/usr/bin/env python3
"""Agent 守护：公网 IP 缓存、内置调度器、WebSocket 长连接 Master."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path

from config import default_install_dir, load_config
from master_public_ip import resolve_public_ip

INSTALL_DIR = default_install_dir()


def main() -> None:
    install = Path(INSTALL_DIR)
    config_path = install / "config.conf"
    cfg = load_config(str(config_path))
    if not cfg:
        sys.exit(1)

    if not cfg.get("TG_TOKEN") or not cfg.get("CHAT_ID"):
        sys.exit(0)

    core = install / "core"
    core.mkdir(parents=True, exist_ok=True)
    ip_cache = core / ".last_ip"

    if not cfg.get("NODE_NAME"):
        raw_ip = cfg.get("PUBLIC_IP", "127.0.0.1")
        ip_hash = hashlib.md5(str(raw_ip).encode()).hexdigest()[:4].upper()
        host = re.sub(r"[^a-zA-Z0-9]", "", socket.gethostname())[:10]
        cfg["NODE_NAME"] = f"{host}-{ip_hash}"

    raw_ip = resolve_public_ip(cfg)
    agent_ip = raw_ip or "Unknown"

    if agent_ip and agent_ip != "Unknown":
        last = ip_cache.read_text(encoding="utf-8").strip() if ip_cache.is_file() else ""
        if agent_ip != last:
            ip_cache.write_text(agent_ip, encoding="utf-8")
            print(f"ℹ️ [Agent] 发现本地 IP 变动，已静默更新缓存: {agent_ip}")
        else:
            print(f"ℹ️ [Agent] IP 未变动 ({agent_ip})，继续后台静默监听。")

    _sched = None
    _ws = None
    try:
        from scheduler import start_scheduler

        _sched = start_scheduler()
        print("✅ [Agent] 内置调度器已启动 (runner / updater / report / quality).")
    except Exception as exc:
        print(f"⚠️  [Agent] 调度器启动失败: {exc}")

    try:
        from agent_ws import start_ws_client

        _ws = start_ws_client()
        print("🚀 [Agent] WSS 客户端已启动（Master 公网经 Telegram 确认）")
    except Exception as exc:
        print(f"❌ [Agent] WebSocket 启动失败: {exc}")
        sys.exit(1)

    _running = True

    def _on_signal(signum, frame):  # noqa: ANN001
        nonlocal _running
        _running = False
        if _sched:
            _sched.stop()
        if _ws:
            _ws.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while _running:
        time.sleep(1)


if __name__ == "__main__":
    main()
