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
from session_stats import record_quality_session
from tg_util import apply_thread, build_svq_callback, escape_markdown, tg_delivery, tg_post

MODULE = "Quality"

_ISO_CC_RE = re.compile(r"^[A-Z]{2}$")
_NON_COUNTRY_ISO = frozenset({"ZH", "EN"})

# 报告展示顺序: (Media 键, 显示名)
_UNLOCK_LINES = (
    ("YoutubePremium", "YouTube Premium"),
    ("GooglePlay", "Google Play"),
    ("Gemini", "Gemini"),
)


def _valid_iso(cc: str) -> str:
    c = (cc or "").strip().upper()
    if _ISO_CC_RE.match(c) and c not in _NON_COUNTRY_ISO:
        return c
    if len(c) == 3 and c.isalpha():
        return c
    return ""


def _resolve_media_iso(data: dict, key: str) -> str:
    media = data.get("Media", {}).get(key, {})
    reg = _valid_iso(media.get("Region") or "")
    if reg:
        return reg
    if key == "Gemini":
        return "--"
    return _valid_iso(data.get("ipCountry", "")) or "--"


def _format_media_iso(data: dict, key: str) -> str:
    media = data.get("Media", {}).get(key, {})
    status = media.get("Status", "未知")
    iso = escape_markdown(_resolve_media_iso(data, key))

    if status == "送中":
        return "🟡 `CN`"
    if status == "N/A":
        return "⚪ `N/A`"
    if status == "解锁":
        return f"🟢 `{iso}`"
    if status == "失败" or iso == "CN":
        if iso == "CN":
            return "🔴 `CN`"
        return f"🔴 `{iso}`"
    if status == "未知":
        return f"⚪ `{iso}`"
    return f"⚪ `{iso}`"


def _google_cn_warning(data: dict) -> str:
    yt = data.get("Media", {}).get("YoutubePremium", {})
    if yt.get("Status") == "送中":
        return "\n🚨 **YouTube 判定为送中（google.cn）。**\n"
    play = data.get("Media", {}).get("GooglePlay", {})
    if play.get("Status") == "失败" and _valid_iso(play.get("Region", "")) == "CN":
        return "\n🚨 **Google Play 判定为中国大陆。**\n"
    return ""


def _tg_post(cfg: dict, payload: dict) -> bool:
    api_url = cfg.get("TG_API_URL", "")
    dest_chat, thread_id = tg_delivery(cfg)
    if not api_url or not dest_chat:
        log(cfg, MODULE, "ERROR", "未配置 TG_API_URL/CHAT_ID，无法推送质量报告")
        return False
    payload = {**payload, "chat_id": dest_chat}
    apply_thread(payload, thread_id)
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
    if name == "IPAPI 风险率" or str(val).endswith("%"):
        return f"• **{name}:** `{escape_markdown(val)}`"
    return f"• **{name}:** `{escape_markdown(val)}/100`"


def _scores_footnote(scam: str, abuse: str, ipqs: str, ip2l: str) -> str:
    core = (scam, abuse, ipqs, ip2l)
    ok = sum(1 for v in core if v != "N/A")
    if ok == 0:
        return "\n_⚠️ 风险数据库均未返回有效分数（出站可能被限或 API 不可用）。_"
    if ok <= 2:
        return "\n_⚠️ 部分风险库未返回数据，仅供参考。_"
    return ""


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

    log(cfg, MODULE, "INFO ", "执行探针: Python ip_quality_probe（jiudu 解锁逻辑）")
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

    unlock_lines = "\n".join(
        f"• **{label}:** {_format_media_iso(data, key)}"
        for key, label in _UNLOCK_LINES
    )

    yt = data.get("Media", {}).get("YoutubePremium", {})
    raw_yt_reg = yt.get("Region", "")
    raw_yt_stat = yt.get("Status", "")
    raw_play = data.get("Media", {}).get("GooglePlay", {}).get("Status", "Unknown")
    raw_gemini = data.get("Media", {}).get("Gemini", {}).get("Status", "未知")

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
            _format_score_label("IPAPI 风险率", str(fraud)),
        ]
    )
    score_note = _scores_footnote(str(scam), str(abuse), str(ipqs), str(ip2l))

    report = f"""🎯 *IP-Sentinel IP 质量报告*
📍 节点：`{safe_alias}`
🌐 地址：`{safe_ip}`{warning}

*🛡️ 风险评分 (越低越好)*
{score_lines}{score_note}

*🎬 核心业务解锁* _(ISO 3166-1 · jiudu)_
{unlock_lines}

*✉️ 邮局与污染度*
• **25 端口出站:** {p25}
• **DNS 污染库:** 严重 `{dns_b}` | 轻微 `{dns_m}`

_👉 [🔍 详细信用图谱直达 (Scamalytics)](https://scamalytics.com/ip/{link_ip})_

⏱️ `{now}` | ⚙️ `v{local_ver}`"""

    safe_scam = re.sub(r"[^0-9]", "", str(scam)) or "0"
    node_name = cfg.get("NODE_NAME", "Unknown")
    cb = build_svq_callback(node_name, safe_scam, raw_yt_reg, raw_yt_stat, raw_play, raw_gemini)

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
        record_quality_session(
            cfg,
            ip=ip_addr,
            scam_score=str(scam),
            youtube_region=raw_yt_reg,
            youtube_status=raw_yt_stat,
            play_status=raw_play,
            gemini_status=raw_gemini,
        )
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
