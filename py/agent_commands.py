"""Agent 指令执行（原 webhook 路由逻辑，供 WebSocket 调用）."""

from __future__ import annotations

import base64
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agent_spawn import spawn_py_script
from config import DEFAULT_INSTALL_DIR, load_config
from log_util import log as agent_log
from log_util import tail_log_file
from task_lock import browser_busy
from tg_util import tg_push

INSTALL_DIR = os.environ.get("IP_SENTINEL_INSTALL_DIR", DEFAULT_INSTALL_DIR)


def _config_path(cfg: dict[str, str]) -> str:
    install = cfg.get("INSTALL_DIR", INSTALL_DIR)
    return f"{install.rstrip('/')}/config.conf"


def _webhook_log(cfg: dict[str, str], level: str, msg: str) -> None:
    agent_log(cfg, "WS", level, msg)


def _config_set_keys(cfg: dict[str, str], updates: dict[str, str]) -> None:
    path = _config_path(cfg)
    with open(path, "r+", encoding="utf-8", errors="ignore") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lines = f.readlines()
        for key, val in updates.items():
            prefix = f"{key}="
            found = False
            for i, line in enumerate(lines):
                if line.startswith(prefix):
                    lines[i] = f'{prefix}"{val}"\n'
                    found = True
                    break
            if not found:
                lines.append(f'{prefix}"{val}"\n')
        f.seek(0)
        f.writelines(lines)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)


def _handle_log_async(cfg: dict[str, str]) -> None:
    try:
        install = cfg.get("INSTALL_DIR", INSTALL_DIR)
        log_path = f"{install}/logs/sentinel.log"
        tail = tail_log_file(log_path, max_lines=15)
        log_data = tail if tail else "日志文件不存在或为空"
        ver = cfg.get("AGENT_VERSION", "未知")
        alias = cfg.get("NODE_ALIAS", cfg.get("NODE_NAME", "Unknown-Node"))
        text = f"📄 <b>[{alias}] 实时日志 (v{ver}):</b>\n<pre><code>{log_data}</code></pre>"
        node_cb = cfg.get("NODE_NAME", "Unknown")
        payload: dict[str, object] = {
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🔄 刷新日志", "callback_data": f"log_refresh:{node_cb}"}],
                    [{"text": "⬅️ 返回控制台", "callback_data": f"manage:{node_cb}"}],
                ]
            },
        }
        if not cfg.get("TG_API_URL") or not cfg.get("CHAT_ID"):
            agent_log(cfg, "WS", "ERROR", "拉取日志：未配置 TG_API_URL/CHAT_ID")
            return
        ok, err = tg_push(cfg, payload, timeout=15)
        if ok:
            agent_log(cfg, "WS", "INFO ", "实时日志已推送")
        else:
            agent_log(cfg, "WS", "WARN ", f"Telegram 日志更新失败: {err}")
    except Exception as exc:
        agent_log(cfg, "WS", "ERROR", f"拉取日志推送失败: {exc}")


def _ota_repo_url(cfg: dict[str, str]) -> str:
    repo_url = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"
    install = cfg.get("INSTALL_DIR", INSTALL_DIR)
    install_sh = f"{install}/core/install.sh"
    if os.path.isfile(install_sh):
        with open(install_sh, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("REPO_RAW_URL="):
                    repo_url = line.split("=", 1)[1].strip().strip('"\'')
                    break
    return repo_url.rstrip("/")


def _write_ota_runner(cfg: dict[str, str], repo_url: str) -> str:
    install = cfg.get("INSTALL_DIR", INSTALL_DIR)
    Path(install, "logs").mkdir(parents=True, exist_ok=True)
    alias = cfg.get("NODE_ALIAS", "未知")
    err_msg = (
        f"❌ **OTA 失败**\n📍 节点: `{alias}`\n"
        "⚠️ 原因: 脚本语法校验(bash -n)未通过，下载可能不完整。\n"
        "🚀 状态: 升级已取消，节点安全。"
    )
    err_b64 = base64.b64encode(err_msg.encode()).decode()
    runner = f"/tmp/ip_sentinel_agent_ota_{os.getpid()}_{int(time.time())}.sh"
    script = f"""#!/bin/bash
set -u
INSTALL_DIR={shlex.quote(install)}
REPO_RAW_URL={shlex.quote(repo_url)}
TG_API_URL={shlex.quote(cfg.get("TG_API_URL", ""))}
CHAT_ID={shlex.quote(cfg.get("CHAT_ID", ""))}
ERR_B64={shlex.quote(err_b64)}
LOG_DIR="$INSTALL_DIR/logs"
mkdir -p "$LOG_DIR" /tmp
LOG_FILE="$LOG_DIR/ota_upgrade.log"
exec >> "$LOG_FILE" 2>&1
echo "========== OTA started $(date -u '+%Y-%m-%d %H:%M:%S UTC') =========="
export SILENT_OTA="true"
export IP_SENTINEL_INSTALL_DIR="$INSTALL_DIR"
export IP_SENTINEL_CONFIG="$INSTALL_DIR/config.conf"
OTA_TMP="/tmp/ota_agent.$$.sh"
cleanup() {{
    rm -f "$OTA_TMP" "$0"
}}
trap cleanup EXIT
if ! curl -fsSL --connect-timeout 10 --retry 3 "$REPO_RAW_URL/core/install.sh" -o "$OTA_TMP"; then
    echo "OTA download failed: $REPO_RAW_URL/core/install.sh"
    exit 1
fi
if [ ! -s "$OTA_TMP" ]; then
    echo "OTA download failed: empty install script"
    exit 1
fi
if bash -n "$OTA_TMP"; then
    bash "$OTA_TMP"
    rc=$?
    echo "========== OTA finished rc=$rc $(date -u '+%Y-%m-%d %H:%M:%S UTC') =========="
    exit "$rc"
fi
MSG=$(printf '%s' "$ERR_B64" | base64 -d 2>/dev/null || true)
if [ -n "$TG_API_URL" ] && [ -n "$CHAT_ID" ] && [ -n "$MSG" ]; then
    curl -s -m 10 -X POST "$TG_API_URL" \\
        -d "chat_id=$CHAT_ID" \\
        --data-urlencode "text=$MSG" \\
        -d "parse_mode=Markdown" >/dev/null 2>&1 || true
fi
echo "OTA syntax check failed: downloaded install script is invalid"
exit 1
"""
    Path(runner).write_text(script, encoding="utf-8")
    os.chmod(runner, 0o700)
    return runner


def _launch_ota_runner(runner: str, cfg: dict[str, str]) -> bool:
    env = {
        **os.environ,
        "IP_SENTINEL_INSTALL_DIR": cfg.get("INSTALL_DIR", INSTALL_DIR),
        "IP_SENTINEL_CONFIG": f"{cfg.get('INSTALL_DIR', INSTALL_DIR).rstrip('/')}/config.conf",
    }
    systemd_run = shutil.which("systemd-run")
    if systemd_run and os.path.isdir("/run/systemd/system"):
        unit = f"ip-sentinel-agent-ota-{os.getpid()}-{int(time.time())}"
        try:
            result = subprocess.run(
                [systemd_run, "--quiet", "--no-block", "--unit", unit, "bash", runner],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                env=env,
            )
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        with open(os.devnull, "rb") as devnull_in, open(os.devnull, "ab") as devnull_out:
            subprocess.Popen(
                ["bash", runner],
                stdin=devnull_in,
                stdout=devnull_out,
                stderr=devnull_out,
                cwd="/",
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        return True
    except OSError:
        return False


def execute_agent_command(path: str, params: dict[str, Any] | None = None) -> tuple[int, str]:
    """执行 Master 下发的指令，返回 (HTTP 风格状态码, 响应体)."""
    params = params or {}
    cfg = load_config()
    if not cfg:
        return 500, "500 Internal Error: config missing\n"

    if path == "/trigger_run":
        _webhook_log(cfg, "INFO ", "收到指令: 立即巡逻")
        if spawn_py_script("runner.py", log_module="WS", extra_env={"IP_SENTINEL_MANUAL_RUN": "1"}):
            return 200, "Action Accepted: runner.py\n"
        return 503, "503 Service Unavailable: runner.py missing\n"

    if path == "/trigger_google":
        _webhook_log(cfg, "INFO ", "收到指令: Google 纠偏")
        if cfg.get("ENABLE_GOOGLE", "false").lower() != "true":
            return 403, "403 Forbidden: Google Module Disabled\n"
        if spawn_py_script("mod_google.py", log_module="WS"):
            return 200, "Action Accepted: mod_google.py\n"
        return 503, "503 Service Unavailable: mod_google.py missing\n"

    if path == "/trigger_trust":
        _webhook_log(cfg, "INFO ", "收到指令: IP 信用净化")
        if cfg.get("ENABLE_TRUST", "false").lower() != "true":
            return 403, "403 Forbidden: Trust Module Disabled\n"
        if spawn_py_script("mod_trust.py", log_module="WS"):
            return 200, "Action Accepted: mod_trust.py\n"
        return 503, "503 Service Unavailable: mod_trust.py missing\n"

    if path == "/trigger_report":
        _webhook_log(cfg, "INFO ", "收到指令: 生成报告")
        if spawn_py_script("report.py", log_module="WS"):
            return 200, "Action Accepted: report.py\n"
        return 503, "503 Service Unavailable: report.py missing\n"

    if path == "/trigger_quality":
        _webhook_log(cfg, "INFO ", "收到指令: IP 质量检测")
        if spawn_py_script("mod_quality.py", log_module="WS"):
            return 200, "Action Accepted: mod_quality.py\n"
        return 503, "503 Service Unavailable: mod_quality.py missing\n"

    if path == "/trigger_log":
        busy, holder = browser_busy()
        if busy:
            _webhook_log(cfg, "INFO ", f"拉取日志；Google 纠偏进行中 (pid={holder})")
        cfg_snapshot = dict(cfg)
        threading.Thread(target=_handle_log_async, args=(cfg_snapshot,), daemon=True).start()
        return 200, "Action Accepted: fetch_log\n"

    if path == "/trigger_set_topic":
        if str(params.get("clear", "")).lower() in ("1", "true", "yes"):
            _config_set_keys(cfg, {"MESSAGE_THREAD_ID": "", "TOPIC_BOT_MESSAGE_ID": ""})
            _webhook_log(cfg, "INFO ", "已清除话题绑定")
            return 200, "Action Accepted: clear_topic\n"
        dest = re.sub(r"[^0-9-]", "", str(params.get("dest_chat", "")))[:20]
        thread_raw = str(params.get("thread_id", ""))
        bot_raw = str(params.get("bot_msg_id", ""))
        if not dest or not thread_raw.isdigit():
            return 400, "400 Bad Request: dest_chat/thread_id required\n"
        try:
            updates = {
                "TG_DEST_CHAT_ID": dest,
                "MESSAGE_THREAD_ID": str(int(thread_raw)),
            }
            if bot_raw.isdigit():
                updates["TOPIC_BOT_MESSAGE_ID"] = str(int(bot_raw))
            _config_set_keys(cfg, updates)
            _webhook_log(cfg, "INFO ", f"已绑定话题: chat={dest} thread={thread_raw}")
            return 200, "Action Accepted: set_topic\n"
        except Exception as exc:
            return 500, f"500 Internal Error: {exc}\n"

    if path == "/trigger_rename":
        b64_alias = str(params.get("b64", ""))
        if not b64_alias:
            return 400, "400 Bad Request: Alias is empty\n"
        try:
            pad = len(b64_alias) % 4
            if pad:
                b64_alias += "=" * (4 - pad)
            b64_alias = b64_alias.replace("-", "+").replace("_", "/")
            raw = base64.b64decode(b64_alias).decode("utf-8", errors="ignore")
            decoded = raw.replace("_", "-")
            safe = re.sub(r"[^a-zA-Z0-9\-\u4e00-\u9fa5]", "", decoded)[:20]
            if not safe:
                return 400, "400 Bad Request: Invalid Characters\n"
            _config_set_keys(cfg, {"NODE_ALIAS": safe})
            _webhook_log(cfg, "INFO ", f"节点别名已更新为: {safe}")
            return 200, "Action Accepted: trigger_rename\n"
        except Exception as exc:
            return 500, f"500 Internal Error: {exc}\n"

    if path == "/trigger_toggle":
        mod_name = str(params.get("mod", ""))
        target_state = str(params.get("state", "")).lower()
        if mod_name not in ("google", "trust") or target_state not in ("true", "false"):
            return 400, "400 Bad Request: Invalid parameters\n"
        key = f"ENABLE_{mod_name.upper()}"
        try:
            _config_set_keys(cfg, {key: target_state})
            _webhook_log(cfg, "INFO ", f"已写入配置 {key}={target_state}")
            return 200, "Action Accepted: trigger_toggle\n"
        except Exception as exc:
            return 500, f"500 Internal Error: {exc}\n"

    if path == "/trigger_ota":
        try:
            _webhook_log(cfg, "INFO ", "收到指令: OTA 升级")
            if cfg.get("ENABLE_OTA", "false").lower() != "true":
                return 403, "403 Forbidden: OTA Upgrade Disabled locally\n"
            if cfg.get("TG_TOKEN") == "OFFICIAL_GATEWAY_MODE":
                return 403, "403 Forbidden: OTA strictly disabled under Public Gateway mode\n"
            runner = _write_ota_runner(cfg, _ota_repo_url(cfg))
            if not _launch_ota_runner(runner, cfg):
                return 500, "500 Internal Error: OTA launcher failed\n"
            return 200, "Action Accepted: trigger_ota\n"
        except Exception as exc:
            return 500, f"500 Internal Error: {exc}\n"

    return 404, "404 Not Found\n"
