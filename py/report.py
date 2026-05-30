#!/usr/bin/env python3
"""Telegram 日报：读取 session_stats 结构化统计（不再解析日志）."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from log_util import log
from network import build_curl_context
from session_stats import latest_snapshot, load_sessions, summarize_google, summarize_trust

MODULE = "Report"
REPO_RAW_URL = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"

FLAGS = {
    "US": "🇺🇸", "JP": "🇯🇵", "HK": "🇭🇰", "TW": "🇹🇼", "SG": "🇸🇬",
    "UK": "🇬🇧", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "NL": "🇳🇱",
    "CA": "🇨🇦", "AU": "🇦🇺", "KR": "🇰🇷", "IN": "🇮🇳", "BR": "🇧🇷",
    "RU": "🇷🇺", "CH": "🇨🇭", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰",
    "FI": "🇫🇮", "IT": "🇮🇹", "ES": "🇪🇸", "PT": "🇵🇹", "IE": "🇮🇪",
    "PL": "🇵🇱", "AT": "🇦🇹", "BE": "🇧🇪", "TR": "🇹🇷", "ZA": "🇿🇦",
    "AE": "🇦🇪", "MY": "🇲🇾", "ID": "🇮🇩", "VN": "🇻🇳", "TH": "🇹🇭",
    "PH": "🇵🇭", "NZ": "🇳🇿", "AR": "🇦🇷", "CL": "🇨🇱", "MX": "🇲🇽",
    "IL": "🇮🇱", "SA": "🇸🇦", "EG": "🇪🇬", "NG": "🇳🇬", "KE": "🇰🇪",
    "RO": "🇷🇴", "BG": "🇧🇬", "CZ": "🇨🇿", "HU": "🇭🇺", "GR": "🇬🇷",
    "UA": "🇺🇦", "MO": "🇲🇴", "KH": "🇰🇭", "MM": "🇲🇲", "LA": "🇱🇦",
    "MN": "🇲🇳", "NP": "🇳🇵", "BD": "🇧🇩",
}


def _node_name(cfg: dict) -> str:
    if cfg.get("NODE_NAME"):
        return cfg["NODE_NAME"]
    ip = cfg.get("PUBLIC_IP", "127.0.0.1")
    ip_hash = hashlib.md5(ip.encode()).hexdigest()[:4].upper()
    host = re.sub(r"[^a-zA-Z0-9]", "", socket.gethostname())[:10]
    return f"{host}-{ip_hash}"


def _fetch_ip(cfg: dict, ctx) -> str:
    for url in ("https://api.ip.sb/ip", "https://ifconfig.me"):
        body = subprocess.run(
            ["curl", *ctx.bind_opt, ctx.ip_flag, "-s", "-m", "5", url],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        ).stdout.strip()
        if body:
            if ":" in body and not body.startswith("["):
                return f"[{body}]"
            return body
    return cfg.get("PUBLIC_IP") or cfg.get("BIND_IP") or ""


def _fetch_isp(cfg: dict, ctx) -> str:
    for url in (
        "https://ipinfo.io/org",
        "https://ip-api.com/line/?fields=isp",
    ):
        info = subprocess.run(
            ["curl", *ctx.bind_opt, ctx.ip_flag, "-s", "-m", "5", url],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        ).stdout.strip()
        if info and "error" not in info.lower():
            break
    else:
        info = ""
    if not info or "error" in info.lower():
        try:
            raw = subprocess.run(
                ["curl", *ctx.bind_opt, ctx.ip_flag, "-s", "-m", "5", "https://api.ip.sb/geoip"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            ).stdout
            info = json.loads(raw).get("organization", "") if raw else ""
        except (json.JSONDecodeError, ValueError):
            info = ""
    info = re.sub(r"^AS\d+\s+", "", info or "")
    if not info or info == "null":
        return "未知 ISP", "未知 ISP 🏠"
    if "Cloudflare" in info:
        return info, "Cloudflare Warp 🛰️"
    return info, f"{info} 🏠"


def _remote_agent_version() -> str:
    try:
        req = urllib.request.Request(
            f"{REPO_RAW_URL}/version.txt",
            headers={"User-Agent": "IP-Sentinel-Agent"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("AGENT_VERSION="):
                    return line.split("=", 1)[1].strip().strip('"')
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return ""


def _send_telegram(cfg: dict, payload: dict) -> bool:
    api_url = cfg.get("TG_API_URL", "")
    if not api_url or not cfg.get("CHAT_ID"):
        log(cfg, MODULE, "ERROR", "未配置 TG_API_URL/CHAT_ID，无法发送报告")
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode(errors="ignore")
            if '"ok":true' in body:
                return True
            log(cfg, MODULE, "WARN ", f"Telegram 返回异常: {body[:200]}")
            return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(cfg, MODULE, "ERROR", f"Telegram 发送失败: {exc}")
        return False


def run() -> int:
    cfg = load_config()
    if not cfg:
        return 1
    log(cfg, MODULE, "START", "========== 生成 Telegram 日报 ==========")
    if not cfg.get("TG_TOKEN") or not cfg.get("CHAT_ID"):
        log(cfg, MODULE, "WARN ", "未配置 Telegram 机器人参数，取消播报")
        return 0

    install = cfg["INSTALL_DIR"]
    lock_file = Path(install) / "core" / ".report_lock"
    now = int(time.time())
    if lock_file.is_file():
        try:
            last = int(lock_file.read_text().strip())
            if now - last < 60:
                log(cfg, MODULE, "WARN ", "报告请求过于频繁，请 60 秒后再试。")
                return 0
        except ValueError:
            pass
    lock_file.write_text(str(now), encoding="utf-8")

    node_name = _node_name(cfg)
    node_alias = cfg.get("NODE_ALIAS") or node_name
    ctx = build_curl_context(cfg)
    current_ip = _fetch_ip(cfg, ctx)
    _, ip_type = _fetch_isp(cfg, ctx)

    base_cc = cfg.get("REGION_CODE", "US").split("-")[0]
    flag = FLAGS.get(base_cc, "🌐")
    region_name = cfg.get("REGION_NAME", base_cc)

    sessions = load_sessions(cfg, hours=24.0)
    snap = latest_snapshot(sessions)

    if not sessions:
        msg = (
            "🛑 **[IP-Sentinel] 告警：节点异常**\n"
            "----------------------------\n"
            f"📍 **节点名称**: `{node_alias}`\n"
            "⚠️ **警告**: 过去 24 小时无会话统计记录！\n"
            "🛠️ **建议**: 节点可能刚安装或尚未完成纠偏/净化，请手动执行一次维护。"
        )
    else:
        msg = (
            f"📊 **IP-Sentinel 每日简报 ({flag} {region_name})**\n"
            "----------------------------\n"
            f"📍 **节点名称**: `{node_alias}`\n"
            f"📡 **出口 IP**: `{current_ip}`\n"
            f"🛡️ **IP 属性**: {ip_type}"
        )

        if cfg.get("ENABLE_GOOGLE", "false").lower() == "true":
            g = summarize_google(sessions)
            msg += (
                f"\n\n🎯 **[Google 区域纠偏]** (过去 24 小时)\n"
                f"🚀 执行总数: {g['total']} 次 (胜率: **{g['rate']}%**)\n"
                f"✅ 成功: {g['ok']} | ❌ CN 判定: {g['fail']} | ⚠️ 警告: {g['warn']}\n"
                f"📍 虚拟定位 Maps: **{g['maps_geo']}** 次 | Earth: **{g['earth_geo']}** 次"
            )

        if cfg.get("ENABLE_TRUST", "false").lower() == "true":
            t = summarize_trust(sessions)
            msg += (
                f"\n\n🔰 **[IP 信用净化]** (过去 24 小时)\n"
                f"🚀 净化总数: {t['total']} 轮 (成功率: **{t['rate']}%**)\n"
                f"✅ 成功注入: {t['ok']} | ❌ 访问受阻: {t['fail']}"
            )

        msg += (
            f"\n\n🕒 **最近执行快照 (过去 24 小时): `{snap['module']}`**\n"
            f"时间: {snap['ts'] or '暂无数据'} (UTC)\n"
            f"结论: {snap['conclusion']}"
        )

    local_ver = cfg.get("AGENT_VERSION", "未知")
    report_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    remote_ver = _remote_agent_version()

    msg += f"\n----------------------------\n🛡️ **系统状态**\n⏱️ 报告时间: `{report_utc}`"
    if remote_ver:
        if remote_ver != local_ver:
            msg += (
                f"\n当前运行版本: `v{local_ver}`\n"
                f"✨ **发现新版本**: `v{remote_ver}` (建议更新)\n"
                "💡 *检测到新版本，建议在 Master 面板执行 OTA 升级。*"
            )
        else:
            msg += (
                f"\n当前运行版本: `v{local_ver}` (✅已是最新)\n"
                "💡 *IP-Sentinel 持续为您守护节点。*\n"
                "*若本项目对您有帮助，欢迎在 GitHub 点 Star。*"
            )
    else:
        msg += (
            f"\n当前运行版本: `v{local_ver}`\n"
            "💡 *IP-Sentinel 持续为您守护节点。*\n"
            "*若本项目对您有帮助，欢迎在 GitHub 点 Star。*"
        )

    payload = {
        "chat_id": cfg["CHAT_ID"],
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node_name}"}]]
        },
    }

    ok = _send_telegram(cfg, payload)
    if ok:
        log(cfg, MODULE, "INFO ", "报告已发送至 Telegram")
    else:
        log(cfg, MODULE, "ERROR", "报告发送失败")
    log(cfg, MODULE, "END  ", "========== 日报任务结束 ==========")
    return 0 if ok else 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
