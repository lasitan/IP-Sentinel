#!/usr/bin/env python3
"""Agent HTTPS Webhook：HMAC 鉴权与模块路由."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from agent_spawn import spawn_py_script
from config import DEFAULT_INSTALL_DIR
from log_util import log as agent_log
from log_util import tail_log_file
from task_lock import maintenance_busy

PY_DIR = Path(__file__).resolve().parent
INSTALL_DIR = os.environ.get("IP_SENTINEL_INSTALL_DIR", DEFAULT_INSTALL_DIR)
CONFIG_PATH = f"{INSTALL_DIR}/config.conf"
CERT_PATH = f"{INSTALL_DIR}/core/cert.pem"
KEY_PATH = f"{INSTALL_DIR}/core/key.pem"

USED_SIGNS: dict[str, float] = {}


def _clean_used_signs() -> None:
    now = time.time()
    expired = [s for s, t in USED_SIGNS.items() if now - t > 65]
    for s in expired:
        del USED_SIGNS[s]


def _auth_token() -> str:
    if not os.path.isfile(CONFIG_PATH):
        return ""
    with open(CONFIG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("CHAT_ID="):
                return line.split("=", 1)[1].strip().strip('"\'')
    return ""


def _load_config_mem() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not os.path.isfile(CONFIG_PATH):
        return cfg
    with open(CONFIG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"\'')
    return cfg


class AgentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        pass

    def _cfg(self) -> dict[str, str]:
        return _load_config_mem()

    def _webhook_log(self, level: str, msg: str) -> None:
        agent_log(self._cfg(), "Webhook", level, msg)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        token = _auth_token()
        if token:
            req_t = query.get("t", [""])[0]
            req_sign = query.get("sign", [""])[0]
            if not req_t or not req_sign:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"401 Unauthorized: Missing Signature\n")
                return
            try:
                if abs(int(time.time()) - int(req_t)) > 60:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"401 Unauthorized: Request Expired\n")
                    return
            except ValueError:
                self.send_response(401)
                self.end_headers()
                return

            _clean_used_signs()
            if req_sign in USED_SIGNS:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"401 Unauthorized: Replay Attack Detected\n")
                return

            msg = f"{req_path}:{req_t}".encode()
            expected = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, req_sign):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"401 Unauthorized: Signature Mismatch\n")
                return
            USED_SIGNS[req_sign] = time.time()

        if req_path == "/trigger_run":
            self._dispatch_spawn("runner.py", "立即巡逻 (/trigger_run)")
            return

        if req_path == "/trigger_google":
            cfg = self._cfg()
            self._webhook_log("INFO ", "收到 Master 指令: Google 纠偏 (/trigger_google)")
            if cfg.get("ENABLE_GOOGLE", "false").lower() != "true":
                self._forbidden(b"403 Forbidden: Google Module Disabled\n")
                return
            self._dispatch_spawn("mod_google.py", "Google 纠偏", log_received=False)
            return

        if req_path == "/trigger_trust":
            cfg = self._cfg()
            self._webhook_log("INFO ", "收到 Master 指令: IP 信用净化 (/trigger_trust)")
            if cfg.get("ENABLE_TRUST", "false").lower() != "true":
                self._forbidden(b"403 Forbidden: Trust Module Disabled\n")
                return
            self._dispatch_spawn("mod_trust.py", "IP 信用净化", log_received=False)
            return

        if req_path == "/trigger_report":
            self._dispatch_spawn("report.py", "生成报告 (/trigger_report)")
            return

        if req_path == "/trigger_log":
            busy, holder = maintenance_busy()
            if busy:
                self._webhook_log(
                    "INFO ",
                    f"收到拉取日志请求；维护任务进行中 (pid={holder})，仅读取日志不启动新任务。",
                )
            else:
                self._webhook_log("INFO ", "收到 Master 指令: 拉取日志 (/trigger_log)")
            self._ok(b"Action Accepted: fetch_log\n")
            cfg_snapshot = self._cfg()
            threading.Thread(
                target=self._handle_log_async,
                args=(cfg_snapshot,),
                daemon=True,
            ).start()
            return

        if req_path == "/trigger_quality":
            self._dispatch_spawn("mod_quality.py", "IP 质量检测 (/trigger_quality)")
            return

        if req_path == "/trigger_rename":
            self._handle_rename(query)
            return

        if req_path == "/trigger_toggle":
            self._handle_toggle(query)
            return

        if req_path == "/trigger_ota":
            self._handle_ota()
            return

        self.send_response(404)
        self.end_headers()

    def _dispatch_spawn(
        self,
        script: str,
        action_label: str,
        *,
        log_received: bool = True,
    ) -> None:
        if log_received:
            self._webhook_log("INFO ", f"收到 Master 指令: {action_label}")
        if spawn_py_script(script, log_module="Webhook"):
            self._ok(f"Action Accepted: {script}\n".encode())
        else:
            self._service_unavailable(script)

    def _ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _forbidden(self, body: bytes) -> None:
        self.send_response(403)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _service_unavailable(self, script: str) -> None:
        self._webhook_log("ERROR", f"拒绝执行：未找到 {script}")
        self.send_response(503)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(f"503 Service Unavailable: {script} missing\n".encode())

    def _handle_log_async(self, cfg: dict[str, str]) -> None:
        """后台拉日志，避免阻塞 HTTPS 线程；不启动任何维护子进程."""
        try:
            install = cfg.get("INSTALL_DIR", INSTALL_DIR)
            log_path = f"{install}/logs/sentinel.log"
            tail = tail_log_file(log_path, max_lines=15)
            log_data = html.escape(tail) if tail else "日志文件不存在或为空"

            ver = cfg.get("AGENT_VERSION", "未知")
            alias = cfg.get("NODE_ALIAS", cfg.get("NODE_NAME", "Unknown-Node"))
            text = f"📄 <b>[{alias}] 实时日志 (v{ver}):</b>\n<pre><code>{log_data}</code></pre>"
            node_cb = cfg.get("NODE_NAME", "Unknown")
            payload = {
                "chat_id": cfg.get("CHAT_ID", ""),
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node_cb}"}]
                    ]
                },
            }
            api_url = cfg.get("TG_API_URL", "")
            if not api_url or not cfg.get("CHAT_ID"):
                agent_log(cfg, "Webhook", "ERROR", "拉取日志：未配置 TG_API_URL/CHAT_ID")
                return
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=data,
                headers={
                    "User-Agent": f"IP-Sentinel-Agent/{ver}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode(errors="ignore")
                if '"ok":true' in body:
                    agent_log(cfg, "Webhook", "INFO ", "实时日志已推送至 Telegram")
                else:
                    agent_log(cfg, "Webhook", "WARN ", f"Telegram 返回异常: {body[:200]}")
        except Exception as exc:
            agent_log(cfg, "Webhook", "ERROR", f"拉取日志推送失败: {exc}")

    def _handle_rename(self, query: dict) -> None:
        self._webhook_log("INFO ", "收到 Master 指令: 重命名节点 (/trigger_rename)")
        b64_alias = query.get("b64", [""])[0]
        if not b64_alias:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"400 Bad Request: Alias is empty\n")
            return
        try:
            pad = len(b64_alias) % 4
            if pad:
                b64_alias += "=" * (4 - pad)
            b64_alias = b64_alias.replace("-", "+").replace("_", "/")
            raw = base64.b64decode(b64_alias).decode("utf-8", errors="ignore")
            decoded = raw.replace("_", "-")
            safe = re.sub(r"[^a-zA-Z0-9\-\u4e00-\u9fa5]", "", decoded)[:20]
            if not safe:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"400 Bad Request: Invalid Characters\n")
                return

            with open(CONFIG_PATH, "r+", encoding="utf-8", errors="ignore") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                lines = f.readlines()
                found = False
                for i, line in enumerate(lines):
                    if line.startswith("NODE_ALIAS="):
                        lines[i] = f'NODE_ALIAS="{safe}"\n'
                        found = True
                        break
                if not found:
                    lines.append(f'NODE_ALIAS="{safe}"\n')
                f.seek(0)
                f.writelines(lines)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)

            self._webhook_log("INFO ", f"节点别名已更新为: {safe}")
            self._ok(b"Action Accepted: trigger_rename\n")
        except Exception as exc:
            self._webhook_log("ERROR", f"重命名失败: {exc}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Error: {exc}\n".encode())

    def _handle_toggle(self, query: dict) -> None:
        mod_name = query.get("mod", [""])[0]
        target_state = query.get("state", [""])[0].lower()
        self._webhook_log(
            "INFO ",
            f"收到 Master 指令: 切换模块 {mod_name}={target_state} (/trigger_toggle)",
        )
        if mod_name not in ("google", "trust") or target_state not in ("true", "false"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"400 Bad Request: Invalid parameters\n")
            return
        key = f"ENABLE_{mod_name.upper()}="
        try:
            with open(CONFIG_PATH, "r+", encoding="utf-8", errors="ignore") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                lines = f.readlines()
                found = False
                for i, line in enumerate(lines):
                    if line.startswith(key):
                        lines[i] = f'{key}"{target_state}"\n'
                        found = True
                        break
                if not found:
                    lines.append(f'{key}"{target_state}"\n')
                f.seek(0)
                f.writelines(lines)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)
            self._webhook_log("INFO ", f"已写入配置 {key}{target_state}")
            self._ok(b"Action Accepted: trigger_toggle\n")
        except Exception as exc:
            self._webhook_log("ERROR", f"切换模块失败: {exc}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Error: {exc}\n".encode())

    def _handle_ota(self) -> None:
        try:
            cfg = self._cfg()
            self._webhook_log("INFO ", "收到 Master 指令: OTA 升级 (/trigger_ota)")
            if cfg.get("ENABLE_OTA", "false").lower() != "true":
                self._forbidden(b"403 Forbidden: OTA Upgrade Disabled locally\n")
                return
            if cfg.get("TG_TOKEN") == "OFFICIAL_GATEWAY_MODE":
                self._forbidden(b"403 Forbidden: OTA strictly disabled under Public Gateway mode\n")
                return

            self._ok(b"Action Accepted: trigger_ota\n")

            repo_url = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"
            install = cfg.get("INSTALL_DIR", INSTALL_DIR)
            install_sh = f"{install}/core/install.sh"
            if os.path.isfile(install_sh):
                with open(install_sh, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("REPO_RAW_URL="):
                            repo_url = line.split("=", 1)[1].strip().strip('"\'')
                            break

            alias = cfg.get("NODE_ALIAS", "未知")
            err_msg = (
                f"❌ **OTA 失败**\n📍 节点: `{alias}`\n"
                "⚠️ 原因: 脚本语法校验(bash -n)未通过，下载可能不完整。\n"
                "🚀 状态: 升级已取消，节点安全。"
            )
            err_b64 = base64.b64encode(err_msg.encode()).decode()
            tg_url = cfg.get("TG_API_URL", "")
            chat_id = cfg.get("CHAT_ID", "")

            ota_script = f"""
export SILENT_OTA="true"
curl -fsSL {repo_url}/core/install.sh -o /tmp/ota_agent.sh
if bash -n /tmp/ota_agent.sh; then
    bash /tmp/ota_agent.sh > {install}/logs/ota_upgrade.log 2>&1
else
    MSG=$(echo '{err_b64}' | base64 -d)
    curl -s -m 10 -X POST "{tg_url}" -d "chat_id={chat_id}" -d "text=$MSG" -d "parse_mode=Markdown" > /dev/null 2>&1
    echo "OTA Checksum Failed: Script corrupted" > {install}/logs/ota_upgrade.log
fi
"""
            ota_b64 = base64.b64encode(ota_script.encode()).decode()
            if shutil.which("systemd-run"):
                cmd = f"systemd-run --quiet --no-block bash -c \"echo '{ota_b64}' | base64 -d | bash\""
            else:
                cmd = f"nohup bash -c \"echo '{ota_b64}' | base64 -d | bash\" >/dev/null 2>&1 &"
            subprocess.Popen(cmd, shell=True, start_new_session=True)
            self._webhook_log("INFO ", "OTA 任务已提交后台执行")
        except Exception as exc:
            self._webhook_log("ERROR", f"OTA 触发失败: {exc}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Error: {exc}\n".encode())


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9527
    bind_addr = "0.0.0.0"
    family = socket.AF_INET

    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("PUBLIC_IP="):
                    pub = line.split("=", 1)[1].strip().strip('"\'')
                    if ":" in pub:
                        bind_addr = "::"
                        family = socket.AF_INET6
                    break

    ThreadedServer.address_family = family
    httpd = ThreadedServer((bind_addr, port), AgentHandler)

    if os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH):
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(certfile=CERT_PATH, keyfile=KEY_PATH)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        except Exception as exc:
            print(f"SSL 隧道构建失败，退化为 HTTP: {exc}")

    try:
        httpd.serve_forever()
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
