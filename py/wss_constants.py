"""Master WSS 硬编码常量（Agent / Master 共用）."""

from __future__ import annotations

MASTER_WSS_PORT = 19530
MASTER_WSS_BIND = "0.0.0.0"


def format_master_host(ip: str) -> str:
    """格式化 WSS URL 主机段（IPv6 加方括号）."""
    raw = (ip or "").strip().strip("[]")
    if not raw:
        return ""
    if ":" in raw:
        return f"[{raw}]"
    return raw


def build_master_wss_url(ip: str) -> str:
    host = format_master_host(ip)
    if not host:
        return ""
    return f"wss://{host}:{MASTER_WSS_PORT}"


def parse_master_wss_line(text: str) -> str:
    """从 ``#MASTER_WSS#|<ip>`` 行解析公网 IP."""
    for line in text.splitlines():
        line = line.strip().replace("`", "")
        if line.startswith("#MASTER_WSS#|"):
            return line.split("|", 1)[1].strip().strip("[]")
    return ""
