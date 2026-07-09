"""Agent 通过 Telegram 确认 Master 公网 WSS 地址."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from config import save_config_keys
from log_util import log
from wss_constants import build_master_wss_url, parse_master_wss_line

MODULE = "WSSResolve"
_LOOKUP_TAG = "#WSS_LOOKUP#"
_CACHE_KEY = "MASTER_PUBLIC_IP"
_IP_RE = re.compile(r"^[\d.a-fA-F:\[\]]+$")


def _tg_api_base(cfg: dict) -> str:
    api = str(cfg.get("TG_API_URL") or "")
    if api.endswith("/sendMessage"):
        return api[: -len("sendMessage")]
    token = str(cfg.get("TG_TOKEN") or "")
    if token and token != "OFFICIAL_GATEWAY_MODE":
        return f"https://api.telegram.org/bot{token}/"
    return ""


def _cached_ip(cfg: dict) -> str:
    return str(cfg.get(_CACHE_KEY) or "").strip().strip("[]")


def _save_cached_ip(cfg: dict, ip: str) -> None:
    clean = ip.strip().strip("[]")
    save_config_keys({_CACHE_KEY: clean})
    cfg[_CACHE_KEY] = clean


def _fetch_ip_from_bot_description(cfg: dict) -> str:
    """从 Bot getMyDescription 读取 Master 写入的 #MASTER_WSS#|ip."""
    base = _tg_api_base(cfg)
    if not base:
        return ""
    try:
        with urllib.request.urlopen(f"{base}getMyDescription", timeout=15) as resp:
            body = json.loads(resp.read().decode(errors="ignore"))
        if not body.get("ok"):
            return ""
        desc = str((body.get("result") or {}).get("description") or "")
        ip = parse_master_wss_line(desc)
        if ip and _IP_RE.match(ip.strip("[]")):
            return ip.strip("[]")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log(cfg, MODULE, "WARN ", f"读取 Bot Description 失败: {exc}")
    return ""


def _notify_master_refresh(cfg: dict, node: str) -> None:
    """通知 Master 刷新公网描述（Master 收到 #WSS_LOOKUP# 后会更新 Description）."""
    chat_id = str(cfg.get("CHAT_ID") or "").strip()
    base = _tg_api_base(cfg)
    if not chat_id or not base:
        return
    payload = json.dumps({"chat_id": chat_id, "text": f"{_LOOKUP_TAG}|{node}"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def request_master_wss_via_tg(cfg: dict, node: str, *, force: bool = False) -> str:
    """
    通过 Telegram 确认 Master 公网 WSS（端口硬编码 19530）：
    1. 读本地缓存 MASTER_PUBLIC_IP
    2. 读 Bot getMyDescription（Master 启动/注册时写入）
    3. 发送 #WSS_LOOKUP# 触发 Master 刷新后重试
    """
    if not force:
        cached = _cached_ip(cfg)
        if cached:
            url = build_master_wss_url(cached)
            if url:
                return url

    ip = _fetch_ip_from_bot_description(cfg)
    if ip:
        _save_cached_ip(cfg, ip)
        url = build_master_wss_url(ip)
        log(cfg, MODULE, "INFO ", f"从 Bot Description 确认 Master WSS: {url}")
        return url

    _notify_master_refresh(cfg, node)
    for _ in range(6):
        time.sleep(3)
        ip = _fetch_ip_from_bot_description(cfg)
        if ip:
            _save_cached_ip(cfg, ip)
            url = build_master_wss_url(ip)
            log(cfg, MODULE, "INFO ", f"Master 公网已通过 TG 确认: {url}")
            return url

    log(cfg, MODULE, "WARN ", "未能通过 TG 确认 Master 公网，请确认 Master 在线且 Bot 可访问")
    cached = _cached_ip(cfg)
    return build_master_wss_url(cached) if cached else ""
