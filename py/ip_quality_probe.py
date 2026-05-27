"""原生 Python IP 质量探针（检测逻辑对齐 xykt/IPQuality）."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from geo_probe import parse_jump_gl, parse_yt_music_gl, parse_yt_premium_gl
from network import CurlContext, build_curl_context, fetch_headers, fetch_text
from persona import DEFAULT_UA

LogFn = Callable[[str, str], None]

_IP_API_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,isp,org,as,mobile,proxy,hosting"
)

# xykt MediaUnlockTest_YouTube_Premium 同款 Cookie（CONSENT + GPS）
_YT_PREMIUM_COOKIE = (
    "YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; GPS=1; "
    "VISITOR_INFO1_LIVE=4VwPMkB7W5A; PREF=tz=Asia.Shanghai"
)

_NETFLIX_TITLES = ("81280792", "70143836")


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
            cc = data.get("country_code") or ""
            return {
                "status": "success",
                "country": data.get("country_name") or data.get("country"),
                "countryCode": cc.upper() if cc else "",
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


def _fetch_scamalytics_score(ip: str, ctx: CurlContext) -> str:
    raw = fetch_text(f"https://ipinfo.check.place/{ip}?db=scamalytics", ctx, timeout=12)
    if raw:
        try:
            data = json.loads(raw)
            score = data.get("scamalytics", {}).get("scamalytics_score")
            if score is not None and str(score) not in ("", "null"):
                return str(int(float(score)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    html = fetch_text(f"https://scamalytics.com/ip/{ip}", ctx, ua=DEFAULT_UA, timeout=20)
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
    return {"Status": "解锁", "Region": region or "", "Type": typ}


def _media_partial(msg: str, region: str = "") -> dict[str, str]:
    return {"Status": msg or "仅部分解锁", "Region": region, "Type": ""}


def _media_block(msg: str = "屏蔽") -> dict[str, str]:
    return {"Status": msg, "Region": "", "Type": ""}


def _probe_youtube(ctx: CurlContext, ua: str) -> tuple[dict[str, str], str]:
    """返回 (media结果, premium_gl). 逻辑对齐 xykt MediaUnlockTest_YouTube_Premium."""
    html = fetch_text(
        "https://www.youtube.com/premium",
        ctx,
        ua=ua,
        timeout=15,
        cookie=_YT_PREMIUM_COOKIE,
        extra_headers=["Accept-Language: en"],
    )
    if not html:
        return _media_block("失败"), ""

    if re.search(r"www\.google\.cn", html):
        return _media_block("中国"), "CN"

    region = parse_yt_premium_gl(html)
    if region == "CN":
        return _media_block("中国"), "CN"

    if "Premium is not available in your country" in html:
        return _media_partial("无Premium", region), region

    if "ad-free" in html and region:
        return _media_unlock(region, "Premium"), region
    if "ad-free" in html:
        return _media_unlock("", "Premium"), region or ""

    return _media_block("失败"), region or ""


def _probe_youtube_music_gl(ctx: CurlContext, ua: str) -> str:
    html = fetch_text("https://music.youtube.com/", ctx, ua=ua, timeout=15)
    if not html:
        return ""
    return parse_yt_music_gl(html)


def _netflix_region_from_body(body: str) -> str:
    m = re.search(r'"id":"([^"]+)".*?"countryName":"[^"]*"', body)
    return m.group(1).upper() if m else ""


def _probe_netflix(ctx: CurlContext, ua: str) -> dict[str, str]:
    """对齐 xykt：检测固定 title 页，而非首页文案."""
    bodies: list[str] = []
    for tid in _NETFLIX_TITLES:
        body = fetch_text(
            f"https://www.netflix.com/title/{tid}",
            ctx,
            ua=ua,
            timeout=15,
            tls13=True,
            fail_on_http_error=True,
        )
        bodies.append(body or "")

    if not all(bodies):
        return _media_block("探测失败")

    region = _netflix_region_from_body(bodies[0]) or _netflix_region_from_body(bodies[1])
    oh_no = [("Oh no!" in b) for b in bodies]

    if all(oh_no):
        return {"Status": "仅自制", "Region": region, "Type": "Streaming"}
    if not any(oh_no):
        return _media_unlock(region, "Streaming")
    return _media_block("未解锁")


def _parse_cf_trace_loc(trace: str) -> str:
    for line in trace.splitlines():
        if line.startswith("loc="):
            return line.split("=", 1)[1].strip().upper()
    return ""


def _probe_chatgpt(ctx: CurlContext, ua: str) -> dict[str, str]:
    """对齐 xykt：以 Cloudflare trace 的 loc= 为准，不单看 chatgpt.com HTTP 码."""
    trace = fetch_text("https://chatgpt.com/cdn-cgi/trace", ctx, timeout=12)
    if not trace:
        trace = fetch_text("https://chat.openai.com/cdn-cgi/trace", ctx, timeout=12)
    loc = _parse_cf_trace_loc(trace)
    if loc == "CN":
        return _media_block("中国")
    if loc and len(loc) == 2:
        return _media_unlock(loc, "Web")
    return _media_partial("待确认", "")


def _probe_tiktok(ctx: CurlContext, ua: str) -> dict[str, str]:
    body = fetch_text("https://www.tiktok.com/", ctx, ua=ua, timeout=15)
    if not body:
        return _media_block("探测失败")
    if "tiktok" in body.lower():
        return _media_unlock("", "")
    return _media_partial("待确认", "")


def _probe_disney(ctx: CurlContext, ua: str) -> dict[str, str]:
    body = fetch_text("https://www.disneyplus.com/", ctx, ua=ua, timeout=15)
    if not body:
        return _media_block("探测失败")
    if "disney" in body.lower() and "not available" not in body.lower():
        return _media_partial("待确认", "")
    if "not available" in body.lower():
        return _media_block("地区不可用")
    return _media_partial("待确认", "")


def _probe_prime(ctx: CurlContext, ua: str) -> dict[str, str]:
    html = fetch_text("https://www.primevideo.com/", ctx, ua=ua, timeout=15)
    if not html:
        return _media_block("探测失败")
    m = re.search(r'"currentTerritory":"([^"]+)"', html)
    if m:
        return _media_unlock(m.group(1).upper(), "Prime")
    return _media_partial("待确认", "")


def _run_media_probes(
    ctx: CurlContext,
    ua: str,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    media: dict[str, dict[str, str]] = {}
    google_geo: dict[str, str] = {}

    jump_hdr = fetch_headers("http://www.google.com/", ctx, timeout=10)
    google_geo["jump"] = parse_jump_gl(jump_hdr)

    yt_media, yt_gl = _probe_youtube(ctx, ua)
    media["Youtube"] = yt_media
    google_geo["premium"] = yt_gl

    tasks = {
        "Netflix": lambda: _probe_netflix(ctx, ua),
        "ChatGPT": lambda: _probe_chatgpt(ctx, ua),
        "TikTok": lambda: _probe_tiktok(ctx, ua),
        "DisneyPlus": lambda: _probe_disney(ctx, ua),
        "AmazonPrimeVideo": lambda: _probe_prime(ctx, ua),
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                media[name] = fut.result()
            except Exception:
                media[name] = _media_block("探测异常")

    google_geo["music"] = _probe_youtube_music_gl(ctx, ua)
    return media, google_geo


def run_quality_probe(cfg: dict[str, Any], log_fn: LogFn | None = None) -> dict[str, Any] | None:
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

    ip_cc = (geo.get("countryCode") or "").upper()
    if not ip_cc and geo.get("country"):
        name = str(geo.get("country", "")).lower()
        if "taiwan" in name:
            ip_cc = "TW"

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
    scam = _fetch_scamalytics_score(ip, ctx)

    _log(log_fn, "INFO ", "Python 探针: Google 三核 + 流媒体（并行）…")
    media, google_geo = _run_media_probes(ctx, ua)
    google_geo["ipCountry"] = ip_cc

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
        "GoogleGeo": google_geo,
        "Mail": {
            "Port25": port25,
            "DNSBlacklist": {"Blacklisted": "0", "Marked": "0", "Total": "0", "Clean": "0"},
        },
    }
