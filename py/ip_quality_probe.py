"""原生 Python IP 质量探针（YouTube / Google Play / Gemini，jiudu 检测逻辑）."""

from __future__ import annotations

import json
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from network import CurlContext, build_curl_context, fetch_text
from persona import DEFAULT_UA

LogFn = Callable[[str, str], None]

_IP_API_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,isp,org,as,mobile,proxy,hosting"
)


_MEDIA_TIMEOUT = 10

_YT_GL_RE = re.compile(r'"INNERTUBE_CONTEXT_GL":"([A-Z]{2})"')
_PLAY_REGION_RE = re.compile(r'"zQmIje":"([A-Z]{2})"')
_GEMINI_REGION_RE = re.compile(r'2,1,200,"([A-Z]{3})"')

_YT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)
_YT_COOKIE = (
    "YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; GPS=1; "
    "VISITOR_INFO1_LIVE=4VwPMkB7W5A; PREF=tz=Asia.Shanghai; _gcl_au=1.1.1809531354.1646633279"
)
_YT_HEADERS = [
    "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
]

_PLAY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
_PLAY_HEADERS = [
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language: zh-CN,zh;q=0.9",
    "Cache-Control: no-cache",
    "Pragma: no-cache",
    "Priority: u=0, i",
    "Upgrade-Insecure-Requests: 1",
    'Sec-Ch-Ua: "Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    'Sec-Ch-Ua-Arch: "x86"',
    'Sec-Ch-Ua-Bitness: "64"',
    'Sec-Ch-Ua-Form-Factors: "Desktop"',
    'Sec-Ch-Ua-Full-Version: "146.0.7680.154"',
    'Sec-Ch-Ua-Full-Version-List: "Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
    "Sec-Ch-Ua-Mobile: ?0",
    'Sec-Ch-Ua-Model: ""',
    'Sec-Ch-Ua-Platform: "Windows"',
    'Sec-Ch-Ua-Platform-Version: "19.0.0"',
    "Sec-Ch-Ua-Wow64: ?0",
    "Sec-Fetch-Dest: document",
    "Sec-Fetch-Mode: navigate",
    "Sec-Fetch-Site: none",
    "Sec-Fetch-User: ?1",
]

_GEMINI_UA = _YT_UA
_GEMINI_COOKIE = (
    "__Secure-BUCKET=CIEC; SEARCH_SAMESITE=CgQIzaAB; AEC=AaJma5soTV08YUyGfUjJv1NkOxzb7GCpo6xwndB3lQdzz2SJBP8bSzZ2GHs; "
    "__utmzz=utmcsr=(direct)|utmcmd=(none)|utmccn=(direct); __utmzzses=1; "
    "NID=530=EMM0EWKz1n0hP2LjRfTdlHSO9WdulkPeZwivqZLlDOtpw12QfNYYLyobn9ZYRVbqAnsIkwniZeDoI_yp_WMVmhUsUA6HblyabKKyC5TYzfmoLr-KwvGUpiUZL-YKkU9DTAujsPLmIZBEYMmgt3q8RFz3ZCTU81LKBxq4pViMaXhtnj0GZUb_7Br3wTxkE7RSn8NFRboQKlQPT3v7DXeey9TJ8kUWMRpQRcM_UK_-7kBrOE8ofUS2r9q54l3elhP2As-z4GfBFKOzqZdGZCa6ojZBxvIR0F8zBVgmTx2vqGEKVfK3XEIkLq8sEXL3RRzJPSOVMS5BCPRF_E7rZgqOKB32jn_BCBo5-X-qeycDrORCihcbspdsjR2U5vWDe6zUXrHCI0In3ezxQbs_nffyV5BPxTcfw-lXIlECO7rm5HHpy13sDRl75mrOBKslC9UAAqRPnpL-voKIrhucOvUz"
)
_GEMINI_HEADERS = [
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
]

_GEMINI_WHITELIST = frozenset(
    {
        "ALB", "DZA", "ASM", "AND", "AGO", "AIA", "ATA", "ATG", "ARG", "ARM", "ABW", "AUS", "AUT", "AZE",
        "BHS", "BHR", "BGD", "BRB", "BEL", "BLZ", "BEN", "BMU", "BTN", "BOL", "BIH", "BWA", "BRA",
        "IOT", "VGB", "BRN", "BGR", "BFA", "BDI", "CPV", "KHM", "CMR", "CAN", "BES", "CYM", "CAF", "TCD",
        "CHL", "CXR", "CCK", "COL", "COM", "COK", "CRI", "CIV", "HRV", "CUW", "CZE", "COD", "DNK", "DJI",
        "DMA", "DOM", "ECU", "EGY", "SLV", "GNQ", "ERI", "EST", "SWZ", "ETH", "FLK", "FRO", "FJI", "FIN",
        "FRA", "GUF", "PYF", "ATF", "GAB", "GMB", "GEO", "DEU", "GHA", "GIB", "GRC", "GRL", "GRD", "GLP",
        "GUM", "GTM", "GGY", "GIN", "GNB", "GUY", "HTI", "HKG", "HMD", "HND", "HUN", "ISL", "IND", "IDN", "IRQ",
        "IRL", "IMN", "ISR", "ITA", "JAM", "JPN", "JEY", "JOR", "KAZ", "KEN", "KIR", "XKX", "KWT", "KGZ",
        "LAO", "LVA", "LBN", "LSO", "LBR", "LBY", "LIE", "LTU", "LUX", "MDG", "MWI", "MYS", "MDV", "MLI",
        "MLT", "MHL", "MTQ", "MRT", "MUS", "MYT", "MEX", "FSM", "MDA", "MCO", "MNG", "MNE", "MSR", "MAR", "MAC",
        "MOZ", "MMR", "NAM", "NRU", "NPL", "NLD", "NCL", "NZL", "NIC", "NER", "NGA", "NIU", "NFK", "MKD",
        "MNP", "NOR", "OMN", "PAK", "PLW", "PSE", "PAN", "PNG", "PRY", "PER", "PHL", "PCN", "POL", "PRT",
        "PRI", "QAT", "CYP", "COG", "REU", "ROU", "RWA", "BLM", "SHN", "KNA", "LCA", "MAF", "SPM", "VCT",
        "WSM", "SMR", "STP", "SAU", "SEN", "SRB", "SYC", "SLE", "SGP", "SXM", "SVK", "SVN", "SLB", "SOM",
        "ZAF", "SGS", "KOR", "SSD", "ESP", "LKA", "SDN", "SUR", "SJM", "SWE", "CHE", "TWN", "TJK", "TZA",
        "THA", "TLS", "TGO", "TKL", "TON", "TTO", "TUN", "TUR", "TKM", "TCA", "TUV", "UGA", "UKR", "ARE",
        "GBR", "USA", "UMI", "URY", "VIR", "UZB", "VUT", "VAT", "VEN", "VNM", "WLF", "ESH", "YEM", "ZMB",
        "ZWE", "ALA",
    }
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


def _media_result(status: str, region: str = "", detail: str = "") -> dict[str, str]:
    return {"Status": status, "Region": region or "", "Type": detail}


def _probe_youtube_premium(ctx: CurlContext) -> dict[str, str]:
    """jiudu YouTube Region Unlock Test."""
    body = fetch_text(
        "https://www.youtube.com/premium",
        ctx,
        ua=_YT_UA,
        cookie=_YT_COOKIE,
        extra_headers=_YT_HEADERS,
        timeout=_MEDIA_TIMEOUT,
    )
    if not body:
        return _media_result("N/A")

    m = _YT_GL_RE.search(body)
    region = m.group(1) if m else ""

    if "www.google.cn" in body:
        return _media_result("送中", "CN")

    if '"content":"YouTube Premium 在你所在的国家/地区尚未推出"' in body:
        return _media_result("解锁", region)

    if (
        '"content":"尽情享受所有 YouTube 内容，不受任何广告干扰。"' in body
        or '"content":"无法享受此优惠"' in body
    ):
        return _media_result("解锁", region)

    return _media_result("未知", region)


def _probe_google_play(ctx: CurlContext) -> dict[str, str]:
    """jiudu Google Play Region Unlock Test."""
    body = fetch_text(
        "https://play.google.com/store/games",
        ctx,
        ua=_PLAY_UA,
        extra_headers=_PLAY_HEADERS,
        timeout=_MEDIA_TIMEOUT,
    )
    if not body:
        return _media_result("N/A")

    m = _PLAY_REGION_RE.search(body)
    region = m.group(1) if m else ""

    if region == "CN":
        return _media_result("失败", region)

    if not region:
        return _media_result("未知")

    return _media_result("解锁", region)


def _probe_gemini(ctx: CurlContext) -> dict[str, str]:
    """jiudu Gemini Region Unlock Test."""
    body = fetch_text(
        "https://gemini.google.com",
        ctx,
        ua=_GEMINI_UA,
        cookie=_GEMINI_COOKIE,
        extra_headers=_GEMINI_HEADERS,
        timeout=_MEDIA_TIMEOUT,
    )
    if not body:
        return _media_result("N/A")

    m = _GEMINI_REGION_RE.search(body)
    country_code = m.group(1) if m else ""

    if not country_code:
        return _media_result("未知")

    if country_code in _GEMINI_WHITELIST:
        return _media_result("解锁", country_code)

    return _media_result("失败", country_code)


def _run_media_probes(ctx: CurlContext) -> dict[str, dict[str, str]]:
    media: dict[str, dict[str, str]] = {}
    tasks = {
        "YoutubePremium": lambda: _probe_youtube_premium(ctx),
        "GooglePlay": lambda: _probe_google_play(ctx),
        "Gemini": lambda: _probe_gemini(ctx),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                media[name] = fut.result()
            except Exception:
                media[name] = _media_result("N/A")
    return media


def _probe_curl_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """质量探针：优先 BIND_IP，否则用 PUBLIC_IP 锁定出口."""
    out = dict(cfg)
    if not (out.get("BIND_IP") or "").strip():
        pub = (out.get("PUBLIC_IP") or "").strip()
        if pub:
            out["BIND_IP"] = pub
    return out


def run_quality_probe(cfg: dict[str, Any], log_fn: LogFn | None = None) -> dict[str, Any] | None:
    probe_cfg = _probe_curl_cfg(cfg)
    ctx = build_curl_context(probe_cfg, lambda lvl, msg: _log(log_fn, lvl, msg))
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

    _log(
        log_fn,
        "INFO ",
        "Python 探针: 流媒体解锁（YouTube / Google Play / Gemini，jiudu，并行）…",
    )
    media = _run_media_probes(ctx)

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


def probe_unlock_cn(ctx: CurlContext) -> tuple[bool, dict[str, dict[str, str]]]:
    """并发探测三大服务（YouTube Premium / Google Play / Gemini）解锁状态。

    返回 ``(is_cn_locked, media)``：

    - ``is_cn_locked = True``：任一服务确认 CN/受限，不应信任地理探针结果。
    - ``media``：各服务原始探针结果，供 ``format_probe_status_line`` 展示。

    判定规则：
    - YouTube Premium 返回「送中」→ CN
    - Google Play 区域为 CN / 状态失败 → CN
    - Gemini 返回「失败」（不在解锁白名单）→ CN/受限
    """
    media = _run_media_probes(ctx)

    yt = media.get("YoutubePremium", {})
    play = media.get("GooglePlay", {})
    gemini = media.get("Gemini", {})

    cn_flags: list[str] = []

    if yt.get("Status") == "送中":
        cn_flags.append("YT")

    if play.get("Region") == "CN" or play.get("Status") == "失败":
        cn_flags.append("Play")

    if gemini.get("Status") == "失败":
        cn_flags.append("Gemini")

    return bool(cn_flags), media


def probe_unlock_cn_retry(
    ctx: CurlContext,
    *,
    attempts: int = 3,
    pause_sec: float = 2.0,
) -> tuple[bool, dict[str, dict[str, str]]]:
    """带重试的解锁检测：浏览器会话后探针易超时，需多次探测."""
    last_media: dict[str, dict[str, str]] = {}
    for i in range(max(1, attempts)):
        locked, media = probe_unlock_cn(ctx)
        last_media = media
        if locked:
            return True, media
        inconclusive = not any(
            (media.get(name) or {}).get("Status") not in ("", "N/A", "未知")
            for name in ("YoutubePremium", "GooglePlay", "Gemini")
        )
        if not inconclusive:
            return False, media
        if i + 1 < attempts:
            time.sleep(pause_sec)
    return False, last_media
