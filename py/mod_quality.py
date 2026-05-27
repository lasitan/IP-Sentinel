#!/usr/bin/env python3
"""深海声呐：IP 质量探针 (委托 xykt/ip_probe 脚本 + JSON 解析)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config import require_config
from network import CurlContext, build_curl_context, preflight

DEFAULT_INSTALL = "/opt/ip_sentinel"
PROBE_URLS = (
    "https://raw.githubusercontent.com/xykt/IPQuality/main/ip.sh",
    "https://IP.Check.Place",
)


def _probe_path(cfg: dict) -> Path:
    return Path(cfg.get("INSTALL_DIR", DEFAULT_INSTALL)) / "core" / "ip_probe.sh"


def _ensure_probe(cfg: dict) -> bool:
    probe_path = _probe_path(cfg)
    if probe_path.is_file():
        try:
            if "xykt" in probe_path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            pass
        probe_path.unlink(missing_ok=True)

    for url in PROBE_URLS:
        subprocess.run(
            ["curl", "-sL", "-m", "15" if "Check" in url else "10", url, "-o", str(probe_path)],
            check=False,
        )
        if probe_path.is_file():
            try:
                if "xykt" in probe_path.read_text(encoding="utf-8", errors="ignore"):
                    probe_path.chmod(0o755)
                    return True
            except OSError:
                pass
        probe_path.unlink(missing_ok=True)
    return False


def _probe_args(cfg: dict) -> list[str]:
    args = ["-y", "-j", "-f"]
    ctx = build_curl_context(cfg)
    raw = (cfg.get("BIND_IP") or "").strip("[]")
    ip_ver = str(ctx.ip_version)

    if raw and ctx.bind_opt:
        args.extend(["-i", raw, f"-{ip_ver}"])
        primary = args[:]
        if preflight(CurlContext(bind_opt=["--interface", cfg["BIND_IP"]], ip_flag=f"-{ip_ver}")):
            return primary
        fallback = ["-y", "-j", f"-{ip_ver}"]
        if preflight(CurlContext(bind_opt=[], ip_flag=f"-{ip_ver}")):
            return fallback
        return ["-y", "-j"]
    args.append(f"-{cfg.get('IP_PREF', '4')}")
    ctx2 = CurlContext(bind_opt=[], ip_flag=f"-{cfg.get('IP_PREF', '4')}")
    if preflight(ctx2):
        return args
    return ["-y", "-j"]


def _strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"x1b\[[0-9;]*[a-zA-Z]", "", text)
    return text


def _run_probe(probe_path: Path, args: list[str]) -> dict | None:
    try:
        raw = subprocess.run(
            ["timeout", "300", "bash", str(probe_path), *args],
            capture_output=True,
            text=True,
            timeout=310,
            check=False,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    raw = _strip_ansi(raw or "")
    idx = raw.find("{")
    if idx < 0:
        return None
    try:
        return json.loads(raw[idx:])
    except json.JSONDecodeError:
        return None


def _parse_media(data: dict, key: str) -> str:
    media = data.get("Media", {}).get(key, {})
    status = media.get("Status", "未知")
    reg = media.get("Region", "")
    typ = media.get("Type", "")
    if "解锁" in status:
        return f"🟢 {reg} ({typ})"
    if any(x in status for x in ("仅", "机房", "待支持")):
        return f"🟡 {status} {reg}"
    if any(x in status for x in ("屏蔽", "失败", "中国", "禁")):
        return f"🔴 {status}"
    return f"⚪ {status}"


def _tg_post(api_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def run() -> int:
    cfg = require_config()
    probe_path = _probe_path(cfg)
    if not _ensure_probe(cfg):
        _tg_post(
            cfg.get("TG_API_URL", ""),
            {
                "chat_id": cfg["CHAT_ID"],
                "parse_mode": "Markdown",
                "text": (
                    "❌ *深海声呐探测失败*\n"
                    f"📍 节点：`{cfg.get('NODE_ALIAS', '未知')}`\n"
                    "⚠️ *探针脚本拉取失败。*"
                ),
            },
        )
        return 1

    args = _probe_args(cfg)
    data = _run_probe(probe_path, args)
    node_alias = cfg.get("NODE_ALIAS") or cfg.get("NODE_NAME", "未知")

    if not data or not data.get("Head", {}).get("IP"):
        _tg_post(
            cfg.get("TG_API_URL", ""),
            {
                "chat_id": cfg["CHAT_ID"],
                "parse_mode": "Markdown",
                "text": (
                    "❌ *深海声呐探测失败*\n"
                    f"📍 节点：`{node_alias}`\n"
                    f"🌐 锁定IP：`{cfg.get('PUBLIC_IP', '')}`\n"
                    "⚠️ *未收到有效回波。检测源超时或数据解析受阻。*"
                ),
            },
        )
        return 1

    info = data.get("Info", {})
    head = data.get("Head", {})
    ip_addr = head.get("IP", "")
    asn = info.get("ASN", "Unknown")
    org = info.get("Organization", "Unknown")
    city = info.get("City", {}).get("Name", "Unknown")
    country = info.get("Region", {}).get("Name", "Unknown")
    ip_type = info.get("Type", "未知属性")
    usage = data.get("Type", {}).get("Usage", {}).get("IPinfo", "未知场景")

    scores = data.get("Score", {})
    scam = scores.get("SCAMALYTICS") or "N/A"
    abuse = scores.get("AbuseIPDB") or "N/A"
    ipqs = scores.get("IPQS") or "N/A"
    ip2l = scores.get("IP2LOCATION") or "N/A"
    fraud = scores.get("ipapi") or "N/A"
    def _clean(v):
        return "N/A" if v in (None, "null", "") else v

    scam, abuse, ipqs, ip2l, fraud = map(_clean, (scam, abuse, ipqs, ip2l, fraud))

    is_proxy = "🟢 干净"
    factor = data.get("Factor", {})
    for section in ("Proxy", "VPN"):
        sec = factor.get(section, {})
        if isinstance(sec, dict) and any(v is True for v in sec.values()):
            is_proxy = "🟡 疑似代理/VPN"
            break

    nf = _parse_media(data, "Netflix")
    yt = _parse_media(data, "Youtube")
    dp = _parse_media(data, "DisneyPlus")
    tk = _parse_media(data, "TikTok")
    gpt = _parse_media(data, "ChatGPT")
    apv = _parse_media(data, "AmazonPrimeVideo")

    raw_yt_reg = data.get("Media", {}).get("Youtube", {}).get("Region", "")
    raw_yt_stat = data.get("Media", {}).get("Youtube", {}).get("Status", "")
    raw_nf = data.get("Media", {}).get("Netflix", {}).get("Status", "Unknown")

    warning = ""
    if raw_yt_reg == "CN" or "中国" in (raw_yt_stat or ""):
        warning = "\n🚨 **[高危] 该节点已被 Google 判定为中国大陆 (送中)！**\n"

    port25 = data.get("Mail", {}).get("Port25") is True
    p25 = "✅ 畅通" if port25 else "❌ 封堵"
    dns = data.get("Mail", {}).get("DNSBlacklist", {})
    dns_b = dns.get("Blacklisted", "0")
    dns_m = dns.get("Marked", "0")

    local_ver = cfg.get("AGENT_VERSION", "未知")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    link_ip = (cfg.get("PUBLIC_IP") or "").strip("[]")

    report = f"""🎯 *IP-Sentinel 深海声呐报告*
📍 节点：`{node_alias}`
🌐 地址：`{ip_addr}`{warning}

*🏢 物理身份与网络属性*
`AS{asn}` | `{org}`
**定位:** `{country} - {city}`
**属性:** `{ip_type}` | `{usage}`
**探针:** {is_proxy}

*🛡️ 欺诈雷达 (0为最优)*
• **Scamalytics:** `{scam}/100`
• **AbuseIPDB:** `{abuse}/100`
• **IPQS:** `{ipqs}/100`
• **IP2Location:** `{ip2l}/100`
• **IPAPI 风险率:** `{fraud}`

*🎬 核心业务解锁*
• **YouTube:** {yt}
• **Netflix:** {nf}
• **Disney+:** {dp}
• **PrimeVideo:** {apv}
• **TikTok:** {tk}
• **ChatGPT:** {gpt}

*✉️ 邮局与污染度*
• **25 端口出站:** {p25}
• **DNS 污染库:** 严重 `{dns_b}` | 轻微 `{dns_m}`

_👉 [🔍 详细信用图谱直达 (Scamalytics)](https://scamalytics.com/ip/{link_ip})_

⏱️ `{now}` | ⚙️ `v{local_ver}`"""

    safe_scam = re.sub(r"[^0-9]", "", str(scam)) or "0"
    raw_goog = raw_yt_reg or raw_yt_stat or "未知"
    raw_gpt = data.get("Media", {}).get("ChatGPT", {}).get("Status", "未知")
    node_name = cfg.get("NODE_NAME", "Unknown")
    cb = f"svq|{node_name}|{safe_scam}|{raw_goog}|{raw_nf}|{raw_gpt}".replace("\n", " ").replace("\r", " ")

    payload = {
        "chat_id": cfg["CHAT_ID"],
        "text": report,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "📥 将本次体检录入趋势库", "callback_data": cb}],
                [{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node_name}"}],
            ]
        },
    }
    _tg_post(cfg.get("TG_API_URL", ""), payload)
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
