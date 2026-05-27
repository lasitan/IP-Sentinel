#!/usr/bin/env python3
"""Telegram 日报：过去 24 小时日志统计、节点状态、版本检查."""

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

from log_util import load_log_lines_within_hours

from config import load_config
from network import build_curl_context

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


_MAPS_GEO_DONE_RE = re.compile(r"\[MAPS_GEO\].*访问完成")
_EARTH_GEO_DONE_RE = re.compile(r"\[EARTH_GEO\].*访问完成")
_MAPS_GEO_SESSION_RE = re.compile(r"本次会话 Maps 虚拟定位访问:\s*(\d+)\s*次")
_EARTH_GEO_SESSION_RE = re.compile(r"本次会话 Earth 虚拟定位访问:\s*(\d+)\s*次")


def _count_tagged_geo_visits(
    google_lines: list[str],
    *,
    done_re: re.Pattern[str],
    session_re: re.Pattern[str],
) -> int:
    per_visit = sum(1 for ln in google_lines if done_re.search(ln))
    if per_visit:
        return per_visit
    session_total = 0
    for ln in google_lines:
        m = session_re.search(ln)
        if m:
            session_total += int(m.group(1))
    return session_total


def _count_maps_geo_visits(google_lines: list[str]) -> int:
    return _count_tagged_geo_visits(
        google_lines, done_re=_MAPS_GEO_DONE_RE, session_re=_MAPS_GEO_SESSION_RE
    )


def _count_earth_geo_visits(google_lines: list[str]) -> int:
    return _count_tagged_geo_visits(
        google_lines, done_re=_EARTH_GEO_DONE_RE, session_re=_EARTH_GEO_SESSION_RE
    )


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


def _send_telegram(api_url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return '"ok":true' in body
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def run() -> int:
    cfg = load_config()
    if not cfg:
        return 1
    if not cfg.get("TG_TOKEN") or not cfg.get("CHAT_ID"):
        print("⚠️ 未配置 Telegram 机器人参数，取消播报。")
        return 0

    install = cfg["INSTALL_DIR"]
    lock_file = Path(install) / "core" / ".report_lock"
    now = int(time.time())
    if lock_file.is_file():
        try:
            last = int(lock_file.read_text().strip())
            if now - last < 60:
                log_path = cfg.get("LOG_FILE", "")
                if log_path:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    ver = cfg.get("AGENT_VERSION", "未知")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(
                            f"[{ts}] [v{ver}] [WARN ] [Report ] [SYSTEM] "
                            "⚠️ 报告请求过于频繁，请 60 秒后再试。\n"
                        )
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

    log_path = Path(cfg.get("LOG_FILE", f"{install}/logs/sentinel.log"))
    day_lines = load_log_lines_within_hours(log_path, hours=24.0)

    if not day_lines:
        msg = (
            "🛑 **[IP-Sentinel] 告警：节点异常**\n"
            "----------------------------\n"
            f"📍 **节点名称**: `{node_alias}`\n"
            "⚠️ **警告**: 过去 24 小时无运行日志！\n"
            "🛠️ **建议**: 节点可能刚安装，请在面板手动执行一次维护任务。"
        )
    else:
        score_lines = [ln for ln in day_lines if "[SCORE]" in ln]
        last_line = score_lines[-1] if score_lines else ""
        last_time = ""
        last_mod = "System"
        last_score = "暂无数据"
        if last_line:
            parts = last_line.split()
            if len(parts) >= 2:
                last_time = parts[0].strip("[]") + " " + parts[1].strip("[]")
            m_mod = re.search(r"\[([^\]]+)\].*\[SCORE\]", last_line)
            if m_mod:
                last_mod = m_mod.group(1).strip()
            if "自检结论: " in last_line:
                last_score = last_line.split("自检结论: ", 1)[1]

        msg = (
            f"📊 **IP-Sentinel 每日简报 ({flag} {region_name})**\n"
            "----------------------------\n"
            f"📍 **节点名称**: `{node_alias}`\n"
            f"📡 **出口 IP**: `{current_ip}`\n"
            f"🛡️ **IP 属性**: {ip_type}"
        )

        if cfg.get("ENABLE_GOOGLE", "false").lower() == "true":
            g_logs = [ln for ln in day_lines if "[Google" in ln]
            g_total = sum(1 for ln in g_logs if "[START]" in ln)
            g_ok = sum(1 for ln in g_logs if "✅" in ln)
            g_fail = sum(1 for ln in g_logs if "❌" in ln)
            g_warn = sum(1 for ln in g_logs if "⚠️" in ln)
            g_maps_geo = _count_maps_geo_visits(g_logs)
            g_earth_geo = _count_earth_geo_visits(g_logs)
            rate = f"{(g_ok / g_total * 100):.1f}" if g_total else "0.0"
            msg += (
                f"\n\n🎯 **[Google 区域纠偏]** (过去 24 小时)\n"
                f"🚀 执行总数: {g_total} 次 (胜率: **{rate}%**)\n"
                f"✅ 成功: {g_ok} | ❌ CN 判定: {g_fail} | ⚠️ 警告: {g_warn}\n"
                f"📍 虚拟定位 Maps: **{g_maps_geo}** 次 | Earth: **{g_earth_geo}** 次"
            )

        if cfg.get("ENABLE_TRUST", "false").lower() == "true":
            t_logs = [ln for ln in day_lines if "[Trust" in ln]
            t_total = sum(1 for ln in t_logs if "[START]" in ln)
            t_ok = sum(1 for ln in t_logs if "✅" in ln)
            t_fail = sum(1 for ln in t_logs if "❌" in ln)
            rate = f"{(t_ok / t_total * 100):.1f}" if t_total else "0.0"
            msg += (
                f"\n\n🔰 **[IP 信用净化]** (过去 24 小时)\n"
                f"🚀 净化总数: {t_total} 轮 (成功率: **{rate}%**)\n"
                f"✅ 成功注入: {t_ok} | ❌ 访问受阻: {t_fail}"
            )

        msg += (
            f"\n\n🕒 **最近执行快照 (过去 24 小时):  `{last_mod}`**\n"
            f"时间: {last_time or '暂无数据'} (UTC)\n"
            f"结论: {last_score}"
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

    ok = _send_telegram(cfg.get("TG_API_URL", ""), payload)
    err_log = Path(install) / "logs" / "error.log"
    if ok:
        print("✅ 报告已发送。")
    else:
        err_log.parent.mkdir(parents=True, exist_ok=True)
        with open(err_log, "a", encoding="utf-8") as f:
            f.write("❌ 报告发送失败。\n")
    return 0 if ok else 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
