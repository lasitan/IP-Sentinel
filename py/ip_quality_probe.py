"""原生 Python IP 质量探针（流媒体解锁对齐 Clash Verge / verge-mihomo）."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from network import CurlContext, build_curl_context, fetch_http_response, fetch_text
from persona import DEFAULT_UA

LogFn = Callable[[str, str], None]

_IP_API_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,isp,org,as,mobile,proxy,hosting"
)


_ISO_CC_RE = re.compile(r"^[A-Z]{2}$")
_NON_COUNTRY_ISO = frozenset({"ZH", "EN"})

_ISO3_TO_ISO2 = {
    "CHN": "CN",
    "HKG": "HK",
    "MAC": "MO",
    "TWN": "TW",
    "USA": "US",
    "GBR": "GB",
    "KOR": "KR",
    "JPN": "JP",
    "AUS": "AU",
    "SGP": "SG",
    "RUS": "RU",
    "DEU": "DE",
    "FRA": "FR",
}

_YT_REGION_PATTERNS = (
    r'id=["\']country-code["\'][^>]*>\s*([A-Za-z]{2,3})\s*<',
    r'"GL"\s*:\s*"([A-Za-z]{2})"',
    r'"countryCode"\s*:\s*"([A-Za-z]{2})"',
    r'"country_code"\s*:\s*"([A-Za-z]{2})"',
)

_GEMINI_BLOCKED = frozenset({"CHN", "RUS", "BLR", "CUB", "IRN", "PRK", "SYR", "HKG", "MAC"})
_GEMINI_REGION_MARKER = ',2,1,200,"'

_CLAUDE_BLOCKED = frozenset({"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"})

_NETFLIX_FAST_URL = (
    "https://api.fast.com/netflix/speedtest/v2"
    "?https=true&token=YXNkZmFzZGxmbnNkYWZoYXNkZmhrYWxm&urlCount=5"
)


def normalize_country_iso(code: str) -> str:
    cc = (code or "").strip().upper()
    if not _ISO_CC_RE.match(cc) or cc in _NON_COUNTRY_ISO:
        return ""
    return cc


def region_label(code: str) -> str:
    """Clash Verge region_label：2/3 位码转 ISO 3166-1 alpha-2."""
    raw = (code or "").strip().upper()
    if not raw:
        return ""
    if len(raw) == 2:
        return normalize_country_iso(raw)
    if len(raw) == 3:
        return normalize_country_iso(_ISO3_TO_ISO2.get(raw, ""))
    return ""


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


def _unlock_result(cv_status: str, region: str = "", detail: str = "") -> dict[str, str]:
    """Clash Verge UnlockItem → 内部结构."""
    status_map = {
        "Yes": "解锁",
        "No": "未解锁",
        "Failed": "失败",
        "Originals Only": "仅自制",
        "Disallowed ISP": "ISP限制",
        "Blocked": "屏蔽",
        "Unsupported Country/Region": "地区不可用",
        "Yes (但无法获取区域)": "解锁",
        "Unknown": "待确认",
    }
    return {
        "Status": status_map.get(cv_status, cv_status),
        "Region": region or "",
        "Type": detail,
    }


def _extract_yt_region(body: str) -> str:
    for pat in _YT_REGION_PATTERNS:
        m = re.search(pat, body, re.I)
        if m:
            cc = region_label(m.group(1))
            if cc:
                return cc
    return ""


def _probe_youtube_premium(ctx: CurlContext, ua: str) -> dict[str, str]:
    url = "https://www.youtube.com/premium?hl=en"
    status, body, _ = fetch_http_response(url, ctx, ua=ua, follow=True, timeout=20)
    if not body:
        return _unlock_result("Failed")

    body_lower = body.lower()
    region = _extract_yt_region(body)

    if (
        "youtube premium is not available in your country" in body_lower
        or "premium is not available in your country" in body_lower
        or "premium is not available in your region" in body_lower
    ):
        return _unlock_result("No", region)

    if 200 <= status < 300 and (
        "youtube premium" in body_lower
        or "ad-free" in body_lower
        or '"browseid":"spunlimited"' in body_lower
    ):
        return _unlock_result("Yes", region)

    return _unlock_result("Failed", region)


def _netflix_fast_com(ctx: CurlContext, ua: str) -> dict[str, str] | None:
    status, body, _ = fetch_http_response(_NETFLIX_FAST_URL, ctx, ua=ua, timeout=30)
    if status == 403:
        return _unlock_result("No", "", "IP Banned")
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    targets = data.get("targets")
    if isinstance(targets, list) and targets:
        loc = targets[0].get("location") or {}
        country = loc.get("country")
        if country:
            return _unlock_result("Yes", region_label(str(country)))
    return None


def _probe_netflix(ctx: CurlContext, ua: str) -> dict[str, str]:
    cdn_hit = _netflix_fast_com(ctx, ua)
    if cdn_hit and cdn_hit.get("Status") == "解锁":
        return cdn_hit

    url1 = "https://www.netflix.com/title/81280792"
    url2 = "https://www.netflix.com/title/70143836"
    status1, _, _ = fetch_http_response(url1, ctx, ua=ua, follow=False, timeout=30)
    status2, _, _ = fetch_http_response(url2, ctx, ua=ua, follow=False, timeout=30)

    if status1 == 0 or status2 == 0:
        return _unlock_result("Failed")

    if status1 == 404 and status2 == 404:
        return _unlock_result("Originals Only")

    if status1 == 403 or status2 == 403:
        return _unlock_result("No")

    if status1 in (200, 301) or status2 in (200, 301):
        test_url = "https://www.netflix.com/title/80018499"
        _, _, headers = fetch_http_response(
            test_url, ctx, ua=ua, follow=False, head_only=True, timeout=30
        )
        location = headers.get("location", "")
        if location:
            parts = location.split("/")
            if len(parts) >= 4:
                region_code = parts[3].split("-")[0]
                cc = region_label(region_code)
                if cc:
                    return _unlock_result("Yes", cc)
        return _unlock_result("Yes", "US")

    return _unlock_result("Failed", detail=f"{status1}_{status2}")


def _probe_gemini(ctx: CurlContext, ua: str) -> dict[str, str]:
    status, body, _ = fetch_http_response(
        "https://gemini.google.com", ctx, ua=ua, follow=True, timeout=20
    )
    if not body:
        return _unlock_result("Failed")

    marker_idx = body.find(_GEMINI_REGION_MARKER)
    country_code = ""
    if marker_idx >= 0:
        start = marker_idx + len(_GEMINI_REGION_MARKER)
        chunk = body[start : start + 3]
        if len(chunk) == 3 and chunk.isascii() and chunk.isalpha() and chunk.isupper():
            country_code = chunk

    if not country_code:
        return _unlock_result("Failed")

    region = region_label(country_code) or country_code
    if country_code in _GEMINI_BLOCKED:
        return _unlock_result("No", region)
    return _unlock_result("Yes", region)


def _tiktok_status_from(status: int, body: str) -> str:
    if status in (403, 451):
        return "No"
    if not (200 <= status < 300):
        return "Failed"
    low = body.lower()
    if (
        "access denied" in low
        or "not available in your region" in low
        or "tiktok is not available" in low
    ):
        return "No"
    return "Yes"


def _tiktok_region_from_body(body: str) -> str:
    m = re.search(r'"region"\s*:\s*"([a-zA-Z-]+)"', body)
    if not m:
        return ""
    raw = m.group(1)
    code = raw.split("-")[0]
    return region_label(code)


def _probe_tiktok(ctx: CurlContext, ua: str) -> dict[str, str]:
    status, body, _ = fetch_http_response(
        "https://www.tiktok.com/cdn-cgi/trace", ctx, ua=ua, follow=True, timeout=20
    )
    cv_status = _tiktok_status_from(status, body)
    region = _tiktok_region_from_body(body)

    if (not region or cv_status == "Failed") and cv_status != "No":
        status2, body2, _ = fetch_http_response(
            "https://www.tiktok.com/", ctx, ua=ua, follow=True, timeout=20
        )
        fb_status = _tiktok_status_from(status2, body2)
        fb_region = _tiktok_region_from_body(body2)
        if cv_status != "No":
            cv_status = fb_status
        if not region:
            region = fb_region

    return _unlock_result(cv_status, region)


def _parse_cf_trace_map(trace: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in trace.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _probe_chatgpt_combined(ctx: CurlContext, ua: str) -> tuple[dict[str, str], dict[str, str]]:
    trace_url = "https://chat.openai.com/cdn-cgi/trace"
    _, trace_body, _ = fetch_http_response(trace_url, ctx, ua=ua, timeout=20)
    if not trace_body:
        _, trace_body, _ = fetch_http_response(
            "https://chatgpt.com/cdn-cgi/trace", ctx, ua=ua, timeout=20
        )
    trace_map = _parse_cf_trace_map(trace_body)
    region = region_label(trace_map.get("loc", ""))

    _, ios_body, _ = fetch_http_response(
        "https://ios.chat.openai.com/", ctx, ua=ua, follow=True, timeout=20
    )
    ios_lower = (ios_body or "").lower()
    if "you may be connected to a disallowed isp" in ios_lower:
        ios_status = "Disallowed ISP"
    elif "request is not allowed. please try again later." in ios_lower:
        ios_status = "Yes"
    elif "sorry, you have been blocked" in ios_lower:
        ios_status = "Blocked"
    else:
        ios_status = "Failed"

    _, web_body, _ = fetch_http_response(
        "https://api.openai.com/compliance/cookie_requirements",
        ctx,
        ua=ua,
        follow=True,
        timeout=20,
    )
    web_lower = (web_body or "").lower()
    if "unsupported_country" in web_lower:
        web_status = "Unsupported Country/Region"
    elif web_body:
        web_status = "Yes"
    else:
        web_status = "Failed"

    return (
        _unlock_result(ios_status, region),
        _unlock_result(web_status, region),
    )


def _probe_claude(ctx: CurlContext, ua: str) -> dict[str, str]:
    _, body, _ = fetch_http_response(
        "https://claude.ai/cdn-cgi/trace", ctx, ua=ua, timeout=20
    )
    if not body:
        return _unlock_result("Failed")

    country_code = ""
    for line in body.splitlines():
        if line.startswith("loc="):
            country_code = line[4:].strip().upper()
            break

    if not country_code:
        return _unlock_result("Failed")

    region = region_label(country_code) or country_code
    if country_code in _CLAUDE_BLOCKED:
        return _unlock_result("No", region)
    return _unlock_result("Yes", region)


def _probe_chatgpt_bundle(ctx: CurlContext, ua: str) -> dict[str, dict[str, str]]:
    ios_item, web_item = _probe_chatgpt_combined(ctx, ua)
    return {"ChatGPT_iOS": ios_item, "ChatGPT_Web": web_item}


def _run_media_probes(ctx: CurlContext, ua: str) -> dict[str, dict[str, str]]:
    media: dict[str, dict[str, str]] = {}
    tasks = {
        "YoutubePremium": lambda: _probe_youtube_premium(ctx, ua),
        "Netflix": lambda: _probe_netflix(ctx, ua),
        "Gemini": lambda: _probe_gemini(ctx, ua),
        "TikTok": lambda: _probe_tiktok(ctx, ua),
        "Claude": lambda: _probe_claude(ctx, ua),
        "ChatGPT": lambda: _probe_chatgpt_bundle(ctx, ua),
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
                if name == "ChatGPT" and isinstance(result, dict):
                    media.update(result)
                else:
                    media[name] = result
            except Exception:
                if name == "ChatGPT":
                    media["ChatGPT_iOS"] = _unlock_result("Failed")
                    media["ChatGPT_Web"] = _unlock_result("Failed")
                else:
                    media[name] = _unlock_result("Failed")
    return media


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

    _log(log_fn, "INFO ", "Python 探针: 多库风险评分…")
    score_map = _fetch_risk_scores(ip, ctx, log_fn)

    _log(log_fn, "INFO ", "Python 探针: 流媒体解锁（Clash Verge 逻辑，并行）…")
    media = _run_media_probes(ctx, ua)

    port25 = _check_port25(cfg)

    return {
        "Head": {"IP": ip},
        "ipCountry": ip_cc,
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
        "Mail": {
            "Port25": port25,
            "DNSBlacklist": {"Blacklisted": "0", "Marked": "0", "Total": "0", "Clean": "0"},
        },
    }
