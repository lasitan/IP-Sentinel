"""原生 Python IP 质量探针（输出与 xykt/IPQuality -j JSON 兼容）."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from geo_probe import parse_yt_premium_gl
from network import CurlContext, build_curl_context, fetch_headers, fetch_text, http_status
from persona import DEFAULT_UA

LogFn = Callable[[str, str], None]

_IP_API_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,isp,org,as,mobile,proxy,hosting"
)


def _log(log_fn: LogFn | None, level: str, msg: str) -> None:
    if log_fn:
        log_fn(level, msg)


def _fetch_ip(ctx: CurlContext) -> str:
    for url in ("https://api.ip.sb/ip", "https://ifconfig.me", "https://api.ipify.org"):
        body = fetch_text(url, ctx, timeout=8).strip()
        if body and re.match(r"^[\d.a-fA-F:\[\]]+$", body):
            return body.strip("[]")
    return ""


def _fetch_ip_api(ip: str, ctx: CurlContext) -> dict[str, Any]:
    for url in (
        f"http://ip-api.com/json/{ip}?fields={_IP_API_FIELDS}",
        f"https://ipapi.co/{ip}/json/",
    ):
        raw = fetch_text(url, ctx, timeout=12)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("status") == "success":
            return data
        if data.get("ip") or data.get("country_name"):
            return {
                "status": "success",
                "country": data.get("country_name") or data.get("country"),
                "city": data.get("city"),
                "org": data.get("org") or data.get("asn"),
                "as": data.get("asn"),
                "mobile": data.get("mobile"),
                "proxy": False,
                "hosting": "hosting" in str(data.get("type", "")).lower(),
            }
    return {}


def _asn_number(as_field: str) -> str:
    m = re.search(r"AS(\d+)", str(as_field or ""), re.I)
    return m.group(1) if m else ""


def _fetch_scamalytics_score(ip: str, ctx: CurlContext, ua: str) -> str:
    html = fetch_text(f"https://scamalytics.com/ip/{ip}", ctx, ua=ua, timeout=20)
    if not html:
        return "N/A"
    for pat in (
        r"Fraud Score:\s*(\d{1,3})",
        r"fraud-score[^>]*>\s*(\d{1,3})",
        r"score[\"']?\s*[:>]\s*(\d{1,3})",
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return "N/A"


def _check_port25(cfg: dict[str, Any]) -> bool | None:
    """
    SMTP 25 出站探测。
    返回 True/False；绑定出口但无法探测时返回 None（未测）.
    """
    bind_ip = (cfg.get("BIND_IP") or "").strip().strip("[]")
    target = ("gmail-smtp-in.l.google.com", 25)
    try:
        if bind_ip and ":" in bind_ip:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.bind((bind_ip, 0, 0, 0))
        elif bind_ip:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((bind_ip, 0))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(target)
        sock.close()
        return True
    except OSError:
        if bind_ip and "." in bind_ip and ":" not in bind_ip:
            return False
        if bind_ip:
            return None
        return False


def _media_unlock(region: str, typ: str = "") -> dict[str, str]:
    return {"Status": "解锁", "Region": region or "未知", "Type": typ}


def _media_partial(msg: str, region: str = "") -> dict[str, str]:
    return {"Status": msg or "仅部分解锁", "Region": region, "Type": ""}


def _media_block(msg: str = "屏蔽") -> dict[str, str]:
    return {"Status": msg, "Region": "", "Type": ""}


def _probe_youtube(ctx: CurlContext, ua: str) -> dict[str, str]:
    html = fetch_text("https://www.youtube.com/premium", ctx, ua=ua, timeout=15)
    if "www.google.cn" in html:
        return _media_block("中国")
    gl = parse_yt_premium_gl(html)
    if gl == "CN":
        return _media_block("中国")
    if gl:
        return _media_unlock(gl, "Premium")
    code = http_status("https://www.youtube.com/premium", ctx, ua=ua, timeout=12)
    if code.startswith("2"):
        return _media_partial("待确认", "")
    return _media_block("失败")


def _probe_netflix(ctx: CurlContext, ua: str) -> dict[str, str]:
    code = http_status("https://www.netflix.com/", ctx, ua=ua, follow=True, timeout=15)
    if not code.startswith("2"):
        return _media_block(f"HTTP {code}")
    html = fetch_text("https://www.netflix.com/", ctx, ua=ua, timeout=15)
    low = html.lower()
    if (
        "not available" in low
        or "不可用" in html
        or ("sorry" in low and "netflix" in low)
        or "unavailable in your country" in low
    ):
        return _media_block("地区不可用")
    return _media_unlock("", "Streaming")


def _probe_chatgpt(ctx: CurlContext, ua: str) -> dict[str, str]:
    code = http_status("https://chatgpt.com/", ctx, ua=ua, follow=True, timeout=15)
    if code.startswith("2"):
        return _media_unlock("", "Web")
    if code in ("403", "451"):
        return _media_block("屏蔽")
    return _media_partial(f"HTTP {code}", "")


def _probe_tiktok(ctx: CurlContext, ua: str) -> dict[str, str]:
    code = http_status("https://www.tiktok.com/", ctx, ua=ua, follow=True, timeout=15)
    if code.startswith("2"):
        return _media_unlock("", "")
    return _media_block(f"HTTP {code}")


def _probe_disney(ctx: CurlContext, ua: str) -> dict[str, str]:
    code = http_status("https://www.disneyplus.com/", ctx, ua=ua, follow=True, timeout=15)
    if code.startswith("2"):
        return _media_partial("待确认", "")
    return _media_block(f"HTTP {code}")


def _probe_prime(ctx: CurlContext, ua: str) -> dict[str, str]:
    hdr = fetch_headers("https://www.primevideo.com/", ctx, timeout=12)
    if "location:" in hdr.lower():
        return _media_unlock("", "Prime")
    code = http_status("https://www.primevideo.com/", ctx, ua=ua, timeout=12)
    if code.startswith("2"):
        return _media_partial("待确认", "")
    return _media_block(f"HTTP {code}")


def _run_media_probes(ctx: CurlContext, ua: str) -> dict[str, dict[str, str]]:
    tasks = {
        "Youtube": lambda: _probe_youtube(ctx, ua),
        "Netflix": lambda: _probe_netflix(ctx, ua),
        "ChatGPT": lambda: _probe_chatgpt(ctx, ua),
        "TikTok": lambda: _probe_tiktok(ctx, ua),
        "DisneyPlus": lambda: _probe_disney(ctx, ua),
        "AmazonPrimeVideo": lambda: _probe_prime(ctx, ua),
    }
    out: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception:
                out[name] = _media_block("探测异常")
    return out


def run_quality_probe(cfg: dict[str, Any], log_fn: LogFn | None = None) -> dict[str, Any] | None:
    """
    执行 IP 质量检测，返回与 IPQuality -j 兼容的 JSON 结构。
    全程 Python + curl，不调用 bash ip_probe.sh。
    """
    ctx = build_curl_context(cfg, lambda lvl, msg: _log(log_fn, lvl, msg))
    ua = DEFAULT_UA

    _log(log_fn, "INFO ", "Python 探针: 获取出口 IP…")
    ip = _fetch_ip(ctx)
    if not ip:
        _log(log_fn, "ERROR", "无法获取出口 IP")
        return None

    _log(log_fn, "INFO ", f"Python 探针: 出口 IP={ip}")
    geo = _fetch_ip_api(ip, ctx)
    if not geo:
        _log(log_fn, "WARN ", "GeoIP 查询失败，使用最小结果集")

    asn_raw = geo.get("as", "") or ""
    org = geo.get("org") or geo.get("isp") or "Unknown"
    city = geo.get("city") or "Unknown"
    country = geo.get("country") or "Unknown"
    mobile = bool(geo.get("mobile"))
    proxy = bool(geo.get("proxy"))
    hosting = bool(geo.get("hosting"))

    if hosting:
        ip_type = "机房/数据中心"
    elif mobile:
        ip_type = "移动网络"
    elif proxy:
        ip_type = "疑似代理"
    else:
        ip_type = "住宅/宽带"

    usage = "机房" if hosting else ("移动" if mobile else "家庭宽带")

    _log(log_fn, "INFO ", "Python 探针: Scamalytics 风险分…")
    scam = _fetch_scamalytics_score(ip, ctx, ua)

    _log(log_fn, "INFO ", "Python 探针: 流媒体解锁检测（并行）…")
    media = _run_media_probes(ctx, ua)

    port25 = _check_port25(cfg)

    return {
        "Head": {"IP": ip},
        "Info": {
            "ASN": _asn_number(asn_raw) or "Unknown",
            "Organization": org,
            "City": {"Name": city},
            "Region": {"Name": country},
            "Type": ip_type,
        },
        "Type": {"Usage": {"IPinfo": usage}},
        "Score": {
            "SCAMALYTICS": scam,
            "AbuseIPDB": "N/A",
            "IPQS": "N/A",
            "IP2LOCATION": "N/A",
            "ipapi": "N/A",
        },
        "Factor": {
            "Proxy": {"ip-api": proxy, "hosting": hosting},
            "VPN": {"ip-api": proxy},
        },
        "Media": media,
        "Mail": {
            "Port25": port25,
            "DNSBlacklist": {"Blacklisted": "0", "Marked": "0", "Total": "0", "Clean": "0"},
        },
    }
