"""输入清洗、SSRF 防护、HMAC 签名 URL."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import time
import urllib.parse

RE_NODE = re.compile(r"[^a-zA-Z0-9_.-]")
RE_REGION = re.compile(r"[^a-zA-Z0-9]")
RE_CHAT = re.compile(r"[^0-9-]")
RE_PORT = re.compile(r"[^0-9]")
RE_SCORE = re.compile(r"[^0-9]")
RE_STATUS = re.compile(r'["\'`$|&;<>\n\r]')


def sanitize_chat_id(chat_id: str) -> str:
    return RE_CHAT.sub("", str(chat_id))


def sanitize_node_name(name: str, max_len: int = 30) -> str:
    return RE_NODE.sub("", name)[:max_len]


def sanitize_region(region: str) -> str:
    return RE_REGION.sub("", region)[:10] or "UNKNOWN"


def sanitize_agent_ip(ip: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.:\[\]-]", "", ip)[:50]


def sanitize_port(port: str) -> str:
    return RE_PORT.sub("", str(port))[:5]


def sanitize_alias(alias: str, max_len: int = 30) -> str:
    cleaned = alias.replace("_", "-")
    cleaned = RE_STATUS.sub("", cleaned)
    return cleaned[:max_len]


def sanitize_status_field(val: str) -> str:
    return RE_STATUS.sub("", val or "")


def sanitize_score(val: str) -> str:
    return RE_SCORE.sub("", val or "")


def is_ssrf_ip(ip: str) -> bool:
    raw = ip.strip("[]").lower()
    if raw in ("localhost", "::1", ""):
        return True
    try:
        addr = ipaddress.ip_address(raw)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return bool(re.match(r"^127\.|^10\.|^192\.168\.|^172\.(1[6-9]|2[0-9]|3[0-1])\.", raw))


def generate_signed_url(auth_key: str, agent_ip: str, agent_port: str, action_path: str) -> str:
    ts = int(time.time())
    payload = f"{action_path}:{ts}"
    sig = hmac.new(auth_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    q = urllib.parse.urlencode({"t": ts, "sign": sig})
    return f"https://{agent_ip}:{agent_port}{action_path}?{q}"


def alias_to_b64(alias: str) -> str:
    raw = base64.b64encode(alias.encode()).decode()
    return raw.rstrip("=").replace("+", "-").replace("/", "_")
