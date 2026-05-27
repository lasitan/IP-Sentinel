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

_ISO_CC_RE = re.compile(r"^[A-Z]{2}$")


def _resolve_media_iso(data: dict, key: str) -> str:
    """从探针结果解析 ISO 3166-1 alpha-2 国家码."""
    media = data.get("Media", {}).get(key, {})
    status = media.get("Status", "")
    reg = (media.get("Region") or "").strip().upper()
    if reg in ("中国", "CHINA"):
        return "CN"
    if _ISO_CC_RE.match(reg):
        return reg
    if status == "中国":
        return "CN"
    g = data.get("GoogleGeo", {})
    if key == "Youtube" and g.get("premium"):
        cc = str(g["premium"]).upper()
        if _ISO_CC_RE.match(cc):
            return cc
    if key in ("Youtube", "TikTok") and g.get("ipCountry"):
        cc = str(g["ipCountry"]).upper()
        if _ISO_CC_RE.match(cc):
            return cc
    return "--"


def _format_media_iso(data: dict, key: str) -> str:
    """核心业务解锁：仅用 ISO 国家码 + 状态色标."""
    media = data.get("Media", {}).get(key, {})
    status = media.get("Status", "未知")
    iso = escape_markdown(_resolve_media_iso(data, key))

    if status == "仅自制":
        return f"🟡 `{iso}`"
    if "解锁" in status:
        return f"🟢 `{iso}`"
    if status == "中国" or iso == "CN":
        return "🔴 `CN`"
    if any(x in status for x in ("屏蔽", "失败", "未解锁", "禁")):
        return f"🔴 `{iso}`"
    if any(x in status for x in ("仅", "待确认", "无Premium", "探测失败")):
        return f"🟡 `{iso}`"
    return f"⚪ `{iso}`"


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

    head = data.get("Head", {})
    ip_addr = head.get("IP", "")

    scores = data.get("Score", {})
    scam = scores.get("SCAMALYTICS") or "N/A"
    abuse = scores.get("AbuseIPDB") or "N/A"
    ipqs = scores.get("IPQS") or "N/A"
    ip2l = scores.get("IP2LOCATION") or "N/A"
    fraud = scores.get("ipapi") or "N/A"

    def _clean(v):
        return "N/A" if v in (None, "null", "") else v

    scam, abuse, ipqs, ip2l, fraud = map(_clean, (scam, abuse, ipqs, ip2l, fraud))

    nf = _format_media_iso(data, "Netflix")
    yt = _format_media_iso(data, "Youtube")
    dp = _format_media_iso(data, "DisneyPlus")
    tk = _format_media_iso(data, "TikTok")
    gpt = _format_media_iso(data, "ChatGPT")
    apv = _format_media_iso(data, "AmazonPrimeVideo")

    raw_yt_reg = data.get("Media", {}).get("Youtube", {}).get("Region", "")
    raw_yt_stat = data.get("Media", {}).get("Youtube", {}).get("Status", "")
    raw_nf = data.get("Media", {}).get("Netflix", {}).get("Status", "Unknown")
    raw_gpt = data.get("Media", {}).get("ChatGPT", {}).get("Status", "未知")

    warning = _google_cn_warning(data)

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

*🛡️ 风险评分 (越低越好)*
{score_lines}

*🎬 核心业务解锁* _(ISO 3166-1)_
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
