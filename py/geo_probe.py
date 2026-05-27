"""Google / YouTube 三核 GeoIP 探针解析."""

from __future__ import annotations

import re

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


def score_geo_status(
    jump_gl: str,
    yt_pr_gl: str,
    yt_mu_gl: str,
    target_cc: str,
) -> str:
    probes = [jump_gl, yt_pr_gl, yt_mu_gl]
    valid = [p for p in probes if p]
    if not valid:
        return "🚨 探针无有效响应（可能被风控拦截）"
    if "CN" in valid:
        return "❌ 严重：多个探针判定 IP 位于中国大陆"

    yt_match = yt_pr_gl == target_cc or yt_mu_gl == target_cc
    if yt_match:
        if jump_gl and jump_gl != target_cc:
            return (
                f"✅ 目标区域匹配 (YouTube 主探针成功, Jump 探针为 {jump_gl}) | "
                f"Prem: {yt_pr_gl or '无'} | Music: {yt_mu_gl or '无'}"
            )
        return (
            f"✅ 目标区域达成 (Jump: {jump_gl or '无'} | "
            f"Prem: {yt_pr_gl or '无'} | Music: {yt_mu_gl or '无'})"
        )
    return (
        f"⚠️ 区域发生漂移！目标 {target_cc}，实际 "
        f"(Jump: {jump_gl or '无'} | Prem: {yt_pr_gl or '无'} | Music: {yt_mu_gl or '无'})"
    )
