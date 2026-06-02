#!/usr/bin/env python3
"""Agent 守护：公网 IP 缓存、TLS 证书、内置调度器、监控并自动重启 Webhook 服务."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from config import default_install_dir, load_config

INSTALL_DIR = default_install_dir()
_WEBHOOK_RESTART_DELAY = 5  # Webhook 意外退出后的重启等待秒数


def _detect_public_ip(ip_pref: str) -> str:
    flag = f"-{ip_pref or '4'}"
    for url in ("https://api.ip.sb/ip", "https://ifconfig.me"):
        try:
            r = subprocess.run(
                ["curl", flag, "-s", "-m", "5", url],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            ip = (r.stdout or "").strip()
            if ip:
                if ":" in ip and not ip.startswith("["):
                    return f"[{ip}]"
                return ip
        except (subprocess.TimeoutExpired, OSError):
            continue
    return ""


def _ensure_tls_certs(core_dir: Path) -> None:
    cert = core_dir / "cert.pem"
    key = core_dir / "key.pem"
    if cert.is_file() and key.is_file():
        return
    print("🔐 [Agent] 正在生成本地自签名 TLS 加密证书 (2048位 RSA)...")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-days",
            "3650",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/C=US/O=IP-Sentinel/CN=Agent-Sec",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _launch_webhook(install: Path, port: int) -> subprocess.Popen:
    """以子进程方式启动 webhook.py，继承环境变量，返回 Popen 对象."""
    webhook = install / "py" / "webhook.py"
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, str(webhook), str(port)],
        cwd=str(install),
        env=env,
    )


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

    raw_ip = _detect_public_ip(cfg.get("IP_PREF", "4"))
    agent_ip = raw_ip or cfg.get("PUBLIC_IP") or cfg.get("BIND_IP") or "Unknown"

    if agent_ip and agent_ip != "Unknown":
        last = ip_cache.read_text(encoding="utf-8").strip() if ip_cache.is_file() else ""
        if agent_ip != last:
            ip_cache.write_text(agent_ip, encoding="utf-8")
            print(f"ℹ️ [Agent] 发现本地 IP 变动，已静默更新缓存: {agent_ip}")
        else:
            print(f"ℹ️ [Agent] IP 未变动 ({agent_ip})，继续后台静默监听。")

    _ensure_tls_certs(core)

    port = int(cfg.get("AGENT_PORT", 9527))
    webhook = install / "py" / "webhook.py"
    if not webhook.is_file():
        print(f"❌ [Agent] 未找到 {webhook}，请重新运行 install.sh")
        sys.exit(1)

    # 启动内置调度器（替代旧 systemd timer / cron）
    _sched = None
    try:
        from scheduler import start_scheduler
        _sched = start_scheduler()
        print("✅ [Agent] 内置调度器已启动 (runner 每 20 分钟 / updater+report 每日).")
    except Exception as exc:
        print(f"⚠️  [Agent] 调度器启动失败，webhook 仍将正常运行: {exc}")

    # 以子进程运行 webhook，并在意外退出后自动重启
    print(f"🚀 [Agent] 正在启动 Webhook 监听服务 (端口: {port})...")
    _running = True

    def _on_signal(signum, frame):  # noqa: ANN001
        nonlocal _running
        _running = False
        if _sched:
            _sched.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    proc = _launch_webhook(install, port)
    while _running:
        try:
            proc.wait()
        except (OSError, subprocess.SubprocessError):
            pass
        if not _running:
            break
        print(f"⚠️  [Agent] Webhook 进程意外退出 (code={proc.returncode})，"
              f"{_WEBHOOK_RESTART_DELAY}s 后重启...")
        time.sleep(_WEBHOOK_RESTART_DELAY)
        cfg = load_config(str(config_path))
        port = int((cfg or {}).get("AGENT_PORT", port))
        proc = _launch_webhook(install, port)

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


if __name__ == "__main__":
    main()
