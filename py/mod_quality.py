#!/usr/bin/env python3
"""IP 质量检测（原生 Python 探针，Telegram 报告）."""

from __future__ import annotations

import re
import sys
import traceback
from datetime import datetime, timezone

from config import require_config
from ip_quality_probe import run_quality_probe
from log_util import log
from tg_util import build_svq_callback, escape_markdown, tg_post

MODULE = "Quality"


def _parse_media(data: dict, key: str) -> str:
    media = data.get("Media", {}).get(key, {})
    status = media.get("Status", "未知")
    reg = media.get("Region", "")
    typ = media.get("Type", "")
    if status == "仅自制":
        label = escape_markdown(reg) if reg else "有区服"
        return f"🟡 仅自制剧 ({label})"
    if "解锁" in status:
        parts = [escape_markdown(reg)] if reg else []
        if typ:
            parts.append(escape_markdown(typ))
        label = " / ".join(parts) if parts else "可访问"
        return f"🟢 {label}"
    if any(x in status for x in ("仅", "机房", "待支持", "待确认", "无Premium")):
        extra = f" {escape_markdown(reg)}" if reg else ""
        return f"🟡 {escape_markdown(status)}{extra}"
    if any(x in status for x in ("屏蔽", "失败", "中国", "禁", "未解锁")):
        return f"🔴 {escape_markdown(status)}"
    return f"⚪ {escape_markdown(status)}"


def _google_cn_warning(data: dict) -> str:
    """三核 + IP 归属交叉验证，避免 TW 节点误报 CN."""
    yt = data.get("Media", {}).get("Youtube", {})
    if yt.get("Status") == "中国":
        return "\n🚨 **Google 地理判定为中国大陆。**\n"
    g = data.get("GoogleGeo", {})
    probes = [g.get("jump"), g.get("premium"), g.get("music")]
    cn_count = sum(1 for p in probes if p == "CN")
    if cn_count < 2:
        return ""
    ip_cc = (g.get("ipCountry") or "").upper()
    if ip_cc in ("TW", "HK", "MO") and cn_count < 3:
        return ""
    return "\n🚨 **Google 多探针判定为中国大陆。**\n"


def _tg_post(cfg: dict, payload: dict) -> bool:
    api_url = cfg.get("TG_API_URL", "")
    chat_id = cfg.get("CHAT_ID", "")
    if not api_url or not chat_id:
        log(cfg, MODULE, "ERROR", "未配置 TG_API_URL/CHAT_ID，无法推送质量报告")
        return False
    payload = {**payload, "chat_id": chat_id}
    ok, err = tg_post(api_url, payload)
    if ok:
        if err == "plain":
            log(cfg, MODULE, "WARN ", "Markdown 解析失败，已降级为纯文本推送")
        else:
            log(cfg, MODULE, "INFO ", "质量报告已推送至 Telegram")
        return True
    log(cfg, MODULE, "ERROR", f"Telegram 推送失败: {err}")
    return False


def _format_score_label(name: str, val: str) -> str:
    if val == "N/A":
        return f"• **{name}:** `N/A`"
    return f"• **{name}:** `{escape_markdown(val)}/100`"


def _port25_label(mail: dict) -> str:
    port25 = mail.get("Port25")
    if port25 is None:
        return "⏸️ 未测（绑定出口或协议不兼容）"
    return "✅ 畅通" if port25 is True else "❌ 封堵"


def _run_inner(cfg: dict) -> int:
    node_alias = cfg.get("NODE_ALIAS") or cfg.get("NODE_NAME", "未知")
    log(cfg, MODULE, "START", f"========== IP 质量检测启动 [节点: {node_alias}] ==========")

    def _probe_log(level: str, msg: str) -> None:
        log(cfg, MODULE, level, msg)

    log(cfg, MODULE, "INFO ", "执行探针: Python ip_quality_probe（原生，无 bash）")
    data = run_quality_probe(cfg, _probe_log)

    if not data or not data.get("Head", {}).get("IP"):
        log(cfg, MODULE, "ERROR", "探针未返回有效结果")
        _tg_post(
            cfg,
            {
                "parse_mode": "Markdown",
                "text": (
                    "❌ *IP 质量检测失败*\n"
                    f"📍 节点：`{escape_markdown(node_alias)}`\n"
                    f"🌐 IP：`{escape_markdown(cfg.get('PUBLIC_IP', ''))}`\n"
                    "⚠️ 探针未返回有效结果（网络或解析失败）。"
                ),
            },
        )
        log(cfg, MODULE, "END  ", "========== IP 质量检测结束 (无有效结果) ==========")
        return 1

    info = data.get("Info", {})
    head = data.get("Head", {})
    ip_addr = head.get("IP", "")
    asn = escape_markdown(info.get("ASN", "Unknown"))
    org = escape_markdown(info.get("Organization", "Unknown"))
    city = escape_markdown(info.get("City", {}).get("Name", "Unknown"))
    country = escape_markdown(info.get("Region", {}).get("Name", "Unknown"))
    ip_type = escape_markdown(info.get("Type", "未知属性"))
    usage = escape_markdown(data.get("Type", {}).get("Usage", {}).get("IPinfo", "未知场景"))

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
    raw_gpt = data.get("Media", {}).get("ChatGPT", {}).get("Status", "未知")

    warning = _google_cn_warning(data)
    ggeo = data.get("GoogleGeo", {})
    geo_line = ""
    if ggeo:
        geo_line = (
            f"\n**Google 三核:** Jump `{escape_markdown(ggeo.get('jump') or '?')}` | "
            f"YT `{escape_markdown(ggeo.get('premium') or '?')}` | "
            f"Music `{escape_markdown(ggeo.get('music') or '?')}`"
        )

    mail = data.get("Mail", {})
    p25 = _port25_label(mail)
    dns = mail.get("DNSBlacklist", {})
    dns_b = escape_markdown(str(dns.get("Blacklisted", "0")))
    dns_m = escape_markdown(str(dns.get("Marked", "0")))

    local_ver = escape_markdown(cfg.get("AGENT_VERSION", "未知"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    link_ip = (cfg.get("PUBLIC_IP") or ip_addr or "").strip("[]")
    safe_ip = escape_markdown(ip_addr)
    safe_alias = escape_markdown(node_alias)

    score_lines = "\n".join(
        [
            _format_score_label("Scamalytics", str(scam)),
            _format_score_label("AbuseIPDB", str(abuse)),
            _format_score_label("IPQS", str(ipqs)),
            _format_score_label("IP2Location", str(ip2l)),
            f"• **IPAPI 风险率:** `{escape_markdown(fraud)}`",
        ]
    )

    report = f"""🎯 *IP-Sentinel IP 质量报告*
📍 节点：`{safe_alias}`
🌐 地址：`{safe_ip}`{warning}

*🏢 物理身份与网络属性*
`AS{asn}` | `{org}`
**定位:** `{country} - {city}`
**属性:** `{ip_type}` | `{usage}`
**探针:** {is_proxy}{geo_line}

*🛡️ 风险评分 (越低越好)*
{score_lines}

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
_⚙️ 探针: Python 原生 · 流媒体并行检测_

⏱️ `{now}` | ⚙️ `v{local_ver}`"""

    safe_scam = re.sub(r"[^0-9]", "", str(scam)) or "0"
    node_name = cfg.get("NODE_NAME", "Unknown")
    cb = build_svq_callback(node_name, safe_scam, raw_yt_reg, raw_yt_stat, raw_nf, raw_gpt)

    payload = {
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
    if _tg_post(cfg, payload):
        log(cfg, MODULE, "SCORE", f"检测完成 IP={ip_addr}")
    else:
        log(cfg, MODULE, "ERROR", "质量报告生成成功但 Telegram 推送失败")
    log(cfg, MODULE, "END  ", "========== IP 质量检测结束 ==========")
    return 0


def run() -> int:
    try:
        cfg = require_config()
    except SystemExit:
        return 1
    try:
        return _run_inner(cfg)
    except Exception as exc:
        try:
            cfg = require_config()
        except SystemExit:
            print(f"[Quality] FATAL: {exc}", file=sys.stderr)
            return 1
        log(cfg, MODULE, "ERROR", f"质量检测未捕获异常: {exc}")
        log(cfg, MODULE, "ERROR", traceback.format_exc().strip()[:800])
        _tg_post(
            cfg,
            {
                "parse_mode": "Markdown",
                "text": (
                    "❌ *IP 质量检测异常退出*\n"
                    f"📍 节点：`{escape_markdown(cfg.get('NODE_ALIAS') or cfg.get('NODE_NAME', '?'))}`\n"
                    f"⚠️ `{escape_markdown(str(exc)[:120])}`"
                ),
            },
        )
        log(cfg, MODULE, "END  ", "========== IP 质量检测结束 (异常) ==========")
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
