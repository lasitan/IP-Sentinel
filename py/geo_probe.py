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
    三大家均返回 CN → 直接认定为中国大陆，不信任其他偏移值。
    """
    sources = [("Gemini", gemini_gl), ("Play", play_gl), ("YouTube", yt_gl)]
    valid = [(n, v) for n, v in sources if v]
    if not valid:
        return "❓ 三大家探针全部超时或无响应"

    cn_names = [n for n, v in valid if v == "CN"]
    valid_vals = [v for _, v in valid]
    status_str = " | ".join(f"{n}: {v}" for n, v in valid)

    # 所有有效探针均为 CN → 确认 CN
    if all(v == "CN" for v in valid_vals):
        return f"❌ 确认 CN：三大家（{' / '.join(n for n, _ in valid)}）均判定中国大陆"

    # 2/3 为 CN
    if len(cn_names) >= 2:
        return f"❌ 大概率 CN：{' / '.join(cn_names)} 判定中国大陆 | {status_str}"

    # 1/3 为 CN
    if cn_names:
        return f"⚠️ 部分探针返回 CN（{cn_names[0]}），建议持续观察 | {status_str}"

    # 全部非 CN，检查是否达到目标区域
    matched = [n for n, v in valid if v == target_cc]
    if matched:
        return f"✅ 三大家区域达成 | {status_str}"

    return f"⚠️ 区域漂移 | 目标 {target_cc} | {status_str}"


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

    # YT 双探针同为 CN → 直接认定，不采信 Jump 偏移值
    # Jump 重定向可被 Maps/Earth/Search 虚拟定位动作影响，YT 内容 region 更可靠
    if yt_pr_gl == "CN" and yt_mu_gl == "CN":
        if jump_gl and jump_gl != "CN":
            return (
                f"❌ 确认 CN：YT 双探针均判定中国大陆，Jump 偏移值（{jump_gl}）不采信"
            )
        return "❌ 确认 CN：三核探针均判定中国大陆"

    if "CN" in valid:
        cn_src = [n for n, v in [("Jump", jump_gl), ("Prem", yt_pr_gl), ("Music", yt_mu_gl)] if v == "CN"]
        return f"⚠️ 部分探针返回 CN（{' / '.join(cn_src)}），建议持续观察"

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
