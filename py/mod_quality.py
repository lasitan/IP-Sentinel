#!/usr/bin/env python3
"""IP 质量检测（Python 入口，对齐深海声呐 bash 报告格式）."""

from __future__ import annotations

import re
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from config import require_config
from ip_quality_probe import run_quality_probe
from log_util import log
from tg_util import build_svq_callback, escape_markdown, tg_post

MODULE = "Quality"

_MEDIA_LINES = (
    ("Youtube", "YouTube"),
    ("Netflix", "Netflix"),
    ("DisneyPlus", "Disney+"),
    ("AmazonPrimeVideo", "PrimeVideo"),
    ("TikTok", "TikTok"),
    ("ChatGPT", "ChatGPT"),
)


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


def _clean_score(val: Any) -> str:
    if val is None or str(val).strip().lower() in ("", "null", "none"):
        return "N/A"
    return str(val)


def _format_score_label(name: str, val: str) -> str:
    if val == "N/A":
        return f"• **{name}:** `N/A`"
    if name == "IPAPI 风险率" or str(val).endswith("%"):
        return f"• **{name}:** `{escape_markdown(val)}`"
    return f"• **{name}:** `{escape_markdown(val)}/100`"


def _scores_footnote(scam: str, abuse: str, ipqs: str, ip2l: str) -> str:
    core = (scam, abuse, ipqs, ip2l)
    ok = sum(1 for v in core if v != "N/A")
    if ok == 0:
        return "\n_⚠️ 风险数据库均未返回有效分数。_"
    if ok <= 2:
        return "\n_⚠️ 部分风险库未返回数据，仅供参考。_"
    return ""


def _media_item(data: dict, key: str) -> dict:
    media = data.get("Media", {})
    if key in media:
        return media[key] or {}
    if key == "Youtube" and "YoutubePremium" in media:
        return media.get("YoutubePremium") or {}
    return {}


def _parse_media_line(data: dict, key: str) -> str:
    """对齐 legacy bash parse_media."""
    media = _media_item(data, key)
    status = str(media.get("Status") or "未知")
    reg = str(media.get("Region") or "").strip()
    typ = str(media.get("Type") or "").strip()

    if "解锁" in status:
        if reg and typ:
            return f"🟢 `{escape_markdown(reg)}` ({escape_markdown(typ)})"
        if reg:
            return f"🟢 `{escape_markdown(reg)}`"
        if typ:
            return f"🟢 ({escape_markdown(typ)})"
        return "🟢 解锁"
    if any(x in status for x in ("仅", "机房", "待支持", "待确认")):
        return f"🟡 {escape_markdown(status)} {escape_markdown(reg)}".strip()
    if any(x in status for x in ("屏蔽", "失败", "中国", "禁", "未解锁")):
        return f"🔴 {escape_markdown(status)}"
    return f"⚪ {escape_markdown(status)}"


def _proxy_label(data: dict) -> str:
    factor = data.get("Factor") or {}

    def _any_true(block: Any) -> bool:
        if not isinstance(block, dict):
            return False
        return any(v is True for v in block.values())

    if _any_true(factor.get("Proxy")) or _any_true(factor.get("VPN")):
        return "🟡 疑似代理/VPN"
    return "🟢 干净"


def _port25_label(mail: dict) -> str:
    port25 = mail.get("Port25")
    if port25 is None:
        return "⏸️ 未测"
    if port25 is True or str(port25).lower() == "true":
        return "✅ 畅通"
    return "❌ 封堵"


def _cn_warning(data: dict) -> str:
    yt = _media_item(data, "Youtube")
    reg = str(yt.get("Region") or "").strip().upper()
    stat = str(yt.get("Status") or "")
    if reg == "CN" or "中国" in stat:
        return "\n🚨 **[高危] 该节点已被 Google 判定为中国大陆 (送中)！**\n"
    return ""


def _run_inner(cfg: dict) -> int:
    node_alias = cfg.get("NODE_ALIAS") or cfg.get("NODE_NAME", "未知")
    log(cfg, MODULE, "START", f"========== IP 质量检测启动 [节点: {node_alias}] ==========")

    def _probe_log(level: str, msg: str) -> None:
        log(cfg, MODULE, level, msg)

    log(cfg, MODULE, "INFO ", "执行探针: xykt/IPQuality（Python 编排，深海声呐逻辑）")
    data = run_quality_probe(cfg, _probe_log)

    if not data or not data.get("Head", {}).get("IP"):
        log(cfg, MODULE, "ERROR", "探针未返回有效结果")
        _tg_post(
            cfg,
            {
                "parse_mode": "Markdown",
                "text": (
                    "❌ *深海声呐探测失败*\n"
                    f"📍 节点：`{escape_markdown(node_alias)}`\n"
                    f"🌐 锁定IP：`{escape_markdown(cfg.get('PUBLIC_IP', ''))}`\n"
                    "⚠️ *未收到有效回波。检测源超时或数据解析受阻。*"
                ),
            },
        )
        log(cfg, MODULE, "END  ", "========== IP 质量检测结束 (无有效结果) ==========")
        return 1

    ip_addr = data.get("Head", {}).get("IP", "")
    info = data.get("Info", {})
    asn = escape_markdown(str(info.get("ASN") or "Unknown"))
    org = escape_markdown(str(info.get("Organization") or "Unknown"))
    city = escape_markdown(str((info.get("City") or {}).get("Name") or "Unknown"))
    country = escape_markdown(str((info.get("Region") or {}).get("Name") or "Unknown"))
    ip_type = escape_markdown(str(info.get("Type") or "未知属性"))
    usage = escape_markdown(str((data.get("Type") or {}).get("Usage", {}).get("IPinfo") or "未知场景"))

    scores = data.get("Score", {})
    scam = _clean_score(scores.get("SCAMALYTICS"))
    abuse = _clean_score(scores.get("AbuseIPDB"))
    ipqs = _clean_score(scores.get("IPQS"))
    ip2l = _clean_score(scores.get("IP2LOCATION"))
    fraud = _clean_score(scores.get("ipapi"))

    unlock_lines = "\n".join(
        f"• **{label}:** {_parse_media_line(data, key)}" for key, label in _MEDIA_LINES
    )

    yt = _media_item(data, "Youtube")
    raw_yt_reg = str(yt.get("Region") or "")
    raw_yt_stat = str(yt.get("Status") or "Unknown")
    raw_nf = str(_media_item(data, "Netflix").get("Status") or "Unknown")
    raw_gpt = str(_media_item(data, "ChatGPT").get("Status") or "未知")

    warning = _cn_warning(data)
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
            _format_score_label("Scamalytics", scam),
            _format_score_label("AbuseIPDB", abuse),
            _format_score_label("IPQS", ipqs),
            _format_score_label("IP2Location", ip2l),
            _format_score_label("IPAPI 风险率", fraud),
        ]
    )
    score_note = _scores_footnote(scam, abuse, ipqs, ip2l)

    report = f"""🎯 *IP-Sentinel 深海声呐报告*
📍 节点：`{safe_alias}`
🌐 地址：`{safe_ip}`{warning}

*🏢 物理身份与网络属性*
`AS{asn}` | `{org}`
**定位:** `{country} - {city}`
**属性:** `{ip_type}` | `{usage}`
**探针:** {_proxy_label(data)}

*🛡️ 欺诈雷达 (0为最优)*
{score_lines}{score_note}

*🎬 核心业务解锁*
{unlock_lines}

*✉️ 邮局与污染度*
• **25 端口出站:** {p25}
• **DNS 污染库:** 严重 `{dns_b}` | 轻微 `{dns_m}`

_👉 [🔍 详细信用图谱直达 (Scamalytics)](https://scamalytics.com/ip/{link_ip})_

⏱️ `{now}` | ⚙️ `v{local_ver}`"""

    safe_scam = re.sub(r"[^0-9]", "", scam) or "0"
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
                    "❌ *深海声呐异常退出*\n"
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
