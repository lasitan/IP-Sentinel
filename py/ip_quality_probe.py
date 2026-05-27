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


_NETFLIX_TITLES = ("81280792", "70143836")

_ISO_CC_RE = re.compile(r"^[A-Z]{2}$")
# 非 ISO 3166-1 国家码（常见于 Netflix 语言/字幕 id，如 zh）
_NON_COUNTRY_ISO = frozenset({"ZH", "EN", "ZH-HANS", "ZH-HANT"})

_COUNTRY_NAME_TO_ISO = {
    "TAIWAN": "TW",
    "HONG KONG": "HK",
    "MACAO": "MO",
    "MACAU": "MO",
    "SINGAPORE": "SG",
    "JAPAN": "JP",
    "UNITED STATES": "US",
    "UNITED KINGDOM": "GB",
    "SOUTH KOREA": "KR",
    "KOREA": "KR",
}

_YT_COOKIE_BASE = (
    "YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; GPS=1; "
    "VISITOR_INFO1_LIVE=4VwPMkB7W5A"
)

_YT_TZ_BY_REGION = {
    "TW": "Asia.Taipei",
    "HK": "Asia.Hong_Kong",
    "MO": "Asia.Macau",
    "JP": "Asia.Tokyo",
    "KR": "Asia.Seoul",
    "SG": "Asia.Singapore",
    "US": "America.New_York",
    "GB": "Europe.London",
    "AU": "Australia.Sydney",
}


def normalize_country_iso(code: str) -> str:
    cc = (code or "").strip().upper()
    if not _ISO_CC_RE.match(cc) or cc in _NON_COUNTRY_ISO:
        return ""
    return cc


def _country_name_to_iso(name: str) -> str:
    key = (name or "").strip().upper()
    return _COUNTRY_NAME_TO_ISO.get(key, "")


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


def _score_int(val: Any) -> str:
    if val is None or str(val).strip().lower() in ("", "null", "none"):
        return "N/A"
    try:
        n = int(float(val))
        if 0 <= n <= 100:
            return str(n)
    except (TypeError, ValueError):
        pass
    return "N/A"


def _fetch_checkplace(ip: str, ctx: CurlContext, db: str) -> dict[str, Any] | None:
    raw = fetch_text(f"https://ipinfo.check.place/{ip}?db={db}", ctx, timeout=14)
    if not raw or not raw.lstrip().startswith("{"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _fetch_ipapi_is_risk(ip: str, ctx: CurlContext) -> str:
    raw = fetch_text(f"https://api.ipapi.is/?q={ip}", ctx, timeout=12)
    if not raw:
        return "N/A"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "N/A"
    scoretext = (data.get("company") or {}).get("abuser_score") or ""
    m = re.match(r"([\d.]+)", str(scoretext))
    if not m:
        return "N/A"
    try:
        pct = float(m.group(1)) * 100
        return f"{pct:.1f}%"
    except ValueError:
        return "N/A"


def _fetch_risk_scores(ip: str, ctx: CurlContext, log_fn: LogFn | None = None) -> dict[str, str]:
    """多库风险分（对齐 xykt / ipinfo.check.place）."""
    scores: dict[str, str] = {
        "SCAMALYTICS": "N/A",
        "AbuseIPDB": "N/A",
        "IPQS": "N/A",
        "IP2LOCATION": "N/A",
        "ipapi": "N/A",
    }

    scam_data = _fetch_checkplace(ip, ctx, "scamalytics")
    if scam_data and "scamalytics" in scam_data:
        scores["SCAMALYTICS"] = _score_int(
            (scam_data.get("scamalytics") or {}).get("scamalytics_score")
        )

    abuse_data = _fetch_checkplace(ip, ctx, "abuseipdb")
    if abuse_data:
        scores["AbuseIPDB"] = _score_int((abuse_data.get("data") or {}).get("abuseConfidenceScore"))

    ipqs_data = _fetch_checkplace(ip, ctx, "ipqualityscore")
    if ipqs_data:
        scores["IPQS"] = _score_int(ipqs_data.get("fraud_score"))

    ip2l_data = _fetch_checkplace(ip, ctx, "ip2location")
    if ip2l_data:
        scores["IP2LOCATION"] = _score_int(ip2l_data.get("fraud_score"))

    scores["ipapi"] = _fetch_ipapi_is_risk(ip, ctx)

    if scam_data is None and abuse_data is None:
        _log(log_fn, "WARN ", "ipinfo.check.place 无响应，尝试 Scamalytics 网页回退…")
        html = fetch_text(f"https://scamalytics.com/ip/{ip}", ctx, ua=DEFAULT_UA, timeout=20)
        for pat in (
            r"Fraud Score:\s*(\d{1,3})",
            r"fraud-score[^>]*>\s*(\d{1,3})",
        ):
            m = re.search(pat, html or "", re.I)
            if m:
                scores["SCAMALYTICS"] = m.group(1)
                break

    ok = sum(1 for v in scores.values() if v != "N/A")
    _log(log_fn, "INFO ", f"Python 探针: 风险库返回 {ok}/5 项有效分数")
    return scores


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


def _yt_premium_cookie(region_code: str) -> str:
    cc = normalize_country_iso(region_code)
    tz = _YT_TZ_BY_REGION.get(cc, "")
    if tz:
        return f"{_YT_COOKIE_BASE}; PREF=tz={tz}"
    return _YT_COOKIE_BASE


def _probe_youtube(
    ctx: CurlContext, ua: str, *, region_code: str = "", ip_cc: str = ""
) -> tuple[dict[str, str], str]:
    """返回 (media结果, premium_gl). 逻辑对齐 xykt MediaUnlockTest_YouTube_Premium."""
    html = fetch_text(
        "https://www.youtube.com/premium",
        ctx,
        ua=ua,
        timeout=15,
        cookie=_yt_premium_cookie(region_code or ip_cc),
        extra_headers=["Accept-Language: en"],
    )
    if not html:
        return _media_block("失败"), ""

    region = parse_yt_premium_gl(html)
    hard_cn = bool(re.search(r"https?://(?:www\.)?google\.cn", html, re.I))

    if hard_cn:
        return _media_block("中国"), "CN"

    if region == "CN" and ip_cc in ("TW", "HK", "MO"):
        if "ad-free" in html:
            region = ip_cc
        else:
            return _media_partial("待确认", ip_cc), ip_cc

    if region == "CN":
        return _media_block("中国"), "CN"

    if "Premium is not available in your country" in html:
        return _media_partial("无Premium", region), region

    if "ad-free" in html and region:
        return _media_unlock(region, "Premium"), region
    if "ad-free" in html and ip_cc:
        return _media_unlock(ip_cc, "Premium"), ip_cc
    if "ad-free" in html:
        return _media_unlock("", "Premium"), region or ""

    return _media_block("失败"), region or ""


def _probe_youtube_music_gl(ctx: CurlContext, ua: str) -> str:
    html = fetch_text("https://music.youtube.com/", ctx, ua=ua, timeout=15)
    if not html:
        return ""
    return parse_yt_music_gl(html)


def _netflix_region_from_body(body: str, fallback_cc: str = "") -> str:
    for pat in (
        r'"countryCode"\s*:\s*"([A-Za-z]{2})"',
        r'"requestCountry"\s*:\s*"([A-Za-z]{2})"',
        r'"currentCountry"\s*:\s*"([A-Za-z]{2})"',
        r'"country"\s*:\s*"([A-Za-z]{2})"',
    ):
        for m in re.finditer(pat, body, re.I):
            cc = normalize_country_iso(m.group(1))
            if cc:
                return cc
    m = re.search(r'"countryName"\s*:\s*"([^"]+)"', body, re.I)
    if m:
        cc = _country_name_to_iso(m.group(1))
        if cc:
            return cc
    return normalize_country_iso(fallback_cc)


def _probe_netflix(ctx: CurlContext, ua: str, *, ip_cc: str = "") -> dict[str, str]:
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

    region = _netflix_region_from_body(bodies[0], ip_cc) or _netflix_region_from_body(bodies[1], ip_cc)
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


def _probe_tiktok(ctx: CurlContext, ua: str, *, ip_cc: str = "") -> dict[str, str]:
    body = fetch_text("https://www.tiktok.com/", ctx, ua=ua, timeout=15)
    if not body:
        return _media_block("探测失败")
    if "tiktok" in body.lower():
        return _media_unlock(ip_cc or "", "")
    return _media_partial("待确认", ip_cc)


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


def _reconcile_youtube_cn(
    media: dict[str, dict[str, str]],
    google_geo: dict[str, str],
    ip_cc: str,
) -> None:
    """IP 归属 TW/HK/MO 时，避免单探针 CN 误报."""
    if ip_cc not in ("TW", "HK", "MO"):
        return
    yt = media.get("Youtube", {})
    if yt.get("Status") != "中国" and google_geo.get("premium") != "CN":
        return
    votes = [
        normalize_country_iso(google_geo.get("premium", "")),
        normalize_country_iso(google_geo.get("music", "")),
        normalize_country_iso(google_geo.get("jump", "")),
        ip_cc,
    ]
    votes = [v for v in votes if v]
    cn_n = sum(1 for v in votes if v == "CN")
    if cn_n >= 2:
        return
    media["Youtube"] = _media_unlock(ip_cc, "Premium")
    google_geo["premium"] = ip_cc


def _run_media_probes(
    ctx: CurlContext,
    ua: str,
    *,
    ip_cc: str = "",
    region_code: str = "",
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    media: dict[str, dict[str, str]] = {}
    google_geo: dict[str, str] = {}

    jump_hdr = fetch_headers("http://www.google.com/", ctx, timeout=10)
    google_geo["jump"] = parse_jump_gl(jump_hdr)

    yt_media, yt_gl = _probe_youtube(ctx, ua, region_code=region_code, ip_cc=ip_cc)
    media["Youtube"] = yt_media
    google_geo["premium"] = yt_gl

    tasks = {
        "Netflix": lambda: _probe_netflix(ctx, ua, ip_cc=ip_cc),
        "ChatGPT": lambda: _probe_chatgpt(ctx, ua),
        "TikTok": lambda: _probe_tiktok(ctx, ua, ip_cc=ip_cc),
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
    _reconcile_youtube_cn(media, google_geo, ip_cc)
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

    region_code = (cfg.get("REGION_CODE") or ip_cc or "").upper()

    _log(log_fn, "INFO ", "Python 探针: 多库风险评分…")
    score_map = _fetch_risk_scores(ip, ctx, log_fn)

    _log(log_fn, "INFO ", "Python 探针: Google 三核 + 流媒体（并行）…")
    media, google_geo = _run_media_probes(ctx, ua, ip_cc=ip_cc, region_code=region_code)
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
        "Score": score_map,
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
