"""Google / YouTube 三核 GeoIP 探针解析."""

from __future__ import annotations

import re
from typing import Any

JUMP_DOMAIN_MAP = {
    "com": "US",
    "com.hk": "HK",
    "com.tw": "TW",
    "co.jp": "JP",
    "co.uk": "GB",
    "co.kr": "KR",
    "co.in": "IN",
    "co.id": "ID",
    "co.th": "TH",
    "com.sg": "SG",
    "com.my": "MY",
    "com.au": "AU",
    "com.br": "BR",
    "com.mx": "MX",
    "com.ar": "AR",
    "co.za": "ZA",
    "cn": "CN",
}


def parse_jump_gl(headers: str) -> str:
    loc = ""
    for line in headers.splitlines():
        if line.lower().startswith("location:"):
            loc = line.split(":", 1)[1].strip()
            break

    if not loc:
        return "US"
    if ".google.cn" in loc or "gl=CN" in loc:
        return "CN"
    m = re.search(r"gl=([A-Za-z]{2})", loc, re.I)
    if m:
        return m.group(1).upper()

    dm = re.search(r"google\.([a-z.]+)", loc, re.I)
    if not dm:
        return ""
    domain = dm.group(1).lower()
    if domain in JUMP_DOMAIN_MAP:
        return JUMP_DOMAIN_MAP[domain]
    if not domain:
        return ""
    last = domain.split(".")[-1].upper()
    if len(last) == 2:
        return last
    return "US"


def _first_regex(html: str, patterns: list[str]) -> str:
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).upper()
    return ""


def parse_yt_premium_gl(html: str) -> str:
    """
    从 YouTube Premium 页解析地区（对齐 xykt：contentRegion + INNERTUBE GL）。
    不使用泛化 countryCode，避免把语言包 CN 误判为地理 CN。
    """
    if re.search(r"www\.google\.cn", html):
        return "CN"
    gl = _first_regex(
        html,
        [
            r'"contentRegion":"([A-Za-z]{2})"',
            r'"INNERTUBE_CONTEXT_GL":"([A-Za-z]{2})"',
        ],
    )
    return gl


def parse_yt_music_gl(html: str) -> str:
    if re.search(r"www\.google\.cn", html):
        return "CN"
    return _first_regex(
        html,
        [
            r'"INNERTUBE_CONTEXT_GL":"([A-Za-z]{2})"',
            r'"GL":"([A-Za-z]{2})"',
            r'"contentRegion":"([A-Za-z]{2})"',
        ],
    )


def target_country_code(region_code: str) -> str:
    cc = region_code.split("-")[0]
    return "GB" if cc == "UK" else cc


def _unlock_probe_value(probe: dict[str, Any] | None) -> str:
    if not probe:
        return "N/A"
    status = str(probe.get("Status") or "N/A")
    region = str(probe.get("Region") or "").strip()
    if region and region not in status:
        return f"{status}({region})"
    return status


def format_probe_status_line(
    *,
    jump_gl: str = "",
    prem_gl: str = "",
    music_gl: str = "",
    yt: dict[str, Any] | None = None,
    play: dict[str, Any] | None = None,
    gemini: dict[str, Any] | None = None,
) -> str:
    """统一探针展示：Jump | Prem | Music | YT | Play | Gemini."""
    return " | ".join(
        [
            f"Jump: {jump_gl or '无'}",
            f"Prem: {prem_gl or '无'}",
            f"Music: {music_gl or '无'}",
            f"YT: {_unlock_probe_value(yt)}",
            f"Play: {_unlock_probe_value(play)}",
            f"Gemini: {_unlock_probe_value(gemini)}",
        ]
    )


# ── 三大家：Gemini / Google Play / YouTube ────────────────────────────────────

def parse_gemini_gl(html: str) -> str:
    """从 Gemini 页面解析区域码（INNERTUBE/gl 模式，CN 域名优先判定）."""
    if not html:
        return ""
    if re.search(r"google\.cn|gemini\.google\.cn", html, re.I):
        return "CN"
    return _first_regex(
        html,
        [
            r'"INNERTUBE_CONTEXT_GL":"([A-Za-z]{2})"',
            r'"userCountryCode":"([A-Za-z]{2})"',
            r'"gl":"([A-Za-z]{2})"',
            r'"countryCode":"([A-Za-z]{2})"',
        ],
    )


def parse_play_gl(html: str) -> str:
    """从 Google Play 页面解析区域码."""
    if not html:
        return ""
    if re.search(r"google\.cn|play\.google\.cn", html, re.I):
        return "CN"
    return _first_regex(
        html,
        [
            r'"STORE_COUNTRY":"([A-Za-z]{2})"',
            r'"gl":"([A-Za-z]{2})"',
            r'"userCountryCode":"([A-Za-z]{2})"',
            r'"countryCode":"([A-Za-z]{2})"',
            r'[?&]gl=([A-Za-z]{2})(?:&|"|\b)',
        ],
    )


def parse_youtube_gl(html: str) -> str:
    """从 YouTube 主页解析区域码（INNERTUBE_CONTEXT_GL / contentRegion）."""
    if not html:
        return ""
    if re.search(r"www\.google\.cn", html, re.I):
        return "CN"
    return _first_regex(
        html,
        [
            r'"INNERTUBE_CONTEXT_GL":"([A-Za-z]{2})"',
            r'"contentRegion":"([A-Za-z]{2})"',
            r'"gl":"([A-Za-z]{2})"',
        ],
    )


def score_three_majors(
    gemini_gl: str,
    play_gl: str,
    yt_gl: str,
    target_cc: str,
) -> str:
    """
    评估 Gemini / Google Play / YouTube 三大家的区域状态。
    任一有效探针返回 CN → 直接 CN 告警，不再记为偏移警告。
    """
    sources = [("Gemini", gemini_gl), ("Play", play_gl), ("YouTube", yt_gl)]
    valid = [(n, v) for n, v in sources if v]
    if not valid:
        return "❓ 三大家探针全部超时或无响应"

    cn_names = [n for n, v in valid if v == "CN"]
    status_str = " | ".join(f"{n}: {v}" for n, v in valid)

    if cn_names:
        return f"❌ CN 告警：{' / '.join(cn_names)} 判定中国大陆 | {status_str}"

    matched = [n for n, v in valid if v == target_cc]
    if matched:
        return f"✅ 三大家区域达成 | {status_str}"

    return f"⚠️ 区域漂移 | 目标 {target_cc} | {status_str}"


def score_geo_status(
    jump_gl: str,
    yt_pr_gl: str,
    yt_mu_gl: str,
    target_cc: str,
    *,
    media: dict[str, dict[str, str]] | None = None,
) -> str:
    line = format_probe_status_line(
        jump_gl=jump_gl,
        prem_gl=yt_pr_gl,
        music_gl=yt_mu_gl,
        yt=(media or {}).get("YoutubePremium"),
        play=(media or {}).get("GooglePlay"),
        gemini=(media or {}).get("Gemini"),
    )
    probes = [jump_gl, yt_pr_gl, yt_mu_gl]
    valid = [p for p in probes if p]
    if not valid:
        return f"🚨 探针无有效响应（可能被风控拦截） | {line}"

    cn_src = [n for n, v in [("Jump", jump_gl), ("Prem", yt_pr_gl), ("Music", yt_mu_gl)] if v == "CN"]
    if cn_src:
        if jump_gl and jump_gl != "CN" and len(cn_src) >= 1:
            return (
                f"❌ CN 告警：{' / '.join(cn_src)} 判定中国大陆（Jump={jump_gl} 不采信）| {line}"
            )
        return f"❌ CN 告警：{' / '.join(cn_src)} 判定中国大陆 | {line}"

    yt_match = yt_pr_gl == target_cc or yt_mu_gl == target_cc
    if yt_match:
        if jump_gl and jump_gl != target_cc:
            return f"✅ 目标区域匹配 (YouTube 主探针成功, Jump 探针为 {jump_gl}) | {line}"
        return f"✅ 目标区域达成 | {line}"
    return f"⚠️ 区域发生漂移！目标 {target_cc} | {line}"
