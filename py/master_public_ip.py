"""Master / Agent 公网 IP 探测（统一实现）."""

from __future__ import annotations

import subprocess
from typing import Any


def detect_public_ip(ip_pref: str = "4") -> str:
    """curl 探测 api.ip.sb / ifconfig.me，与 Agent 安装/运行时一致."""
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


def resolve_public_ip(cfg: dict[str, Any] | None = None, *, ip_pref: str | None = None) -> str:
    """curl 探测 → PUBLIC_IP → BIND_IP，与 Agent 逻辑一致."""
    pref = str(ip_pref or (cfg or {}).get("IP_PREF") or "4")
    ip = detect_public_ip(pref)
    if ip:
        return ip
    if cfg:
        for key in ("PUBLIC_IP", "BIND_IP"):
            val = str(cfg.get(key) or "").strip()
            if val:
                return val
    return ""


def normalize_ip_for_storage(ip: str) -> str:
    """存储或写入 Bot Description 时使用裸 IP（无方括号）."""
    return ip.strip().strip("[]")
