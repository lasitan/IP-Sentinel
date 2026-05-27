#!/usr/bin/env python3
"""Agent 守护：公网 IP 缓存、TLS 证书、启动 Webhook 服务."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from config import load_config

INSTALL_DIR = os.environ.get("IP_SENTINEL_INSTALL_DIR", "/opt/ip_sentinel")


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

    print(f"🚀 [Agent] 正在启动 Webhook 监听服务 (端口: {port})...")
    os.execv(sys.executable, [sys.executable, str(webhook), str(port)])


if __name__ == "__main__":
    main()
