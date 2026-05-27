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
import time
import urllib.parse
import urllib.request
from pathlib import Path

from config import DEFAULT_INSTALL_DIR

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


def _spawn_py(script: str) -> None:
    path = PY_DIR / script
    if path.is_file():
        subprocess.Popen(
            [sys.executable, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _spawn_runner() -> None:
    runner = PY_DIR / "runner.py"
    if runner.is_file():
        subprocess.Popen(
            [sys.executable, str(runner)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class AgentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        pass

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
            if (PY_DIR / "runner.py").is_file():
                self._ok(b"Action Accepted: runner\n")
                _spawn_runner()
            else:
                self.send_response(404)
                self.end_headers()
            return

        if req_path == "/trigger_google":
            if (PY_DIR / "mod_google.py").is_file():
                self._ok(b"Action Accepted: mod_google\n")
                _spawn_py("mod_google.py")
            else:
                self._forbidden(b"403 Forbidden: Google Module Disabled\n")
            return

        if req_path == "/trigger_trust":
            if (PY_DIR / "mod_trust.py").is_file():
                self._ok(b"Action Accepted: mod_trust\n")
                _spawn_py("mod_trust.py")
            else:
                self._forbidden(b"403 Forbidden: Trust Module Disabled\n")
            return

        if req_path == "/trigger_report":
            self._ok(b"Action Accepted: tg_report\n")
            _spawn_py("report.py")
            return

        if req_path == "/trigger_log":
            self._ok(b"Action Accepted: fetch_log\n")
            self._handle_log()
            return

        if req_path == "/trigger_quality":
            self._ok(b"Action Accepted: trigger_quality\n")
            if (PY_DIR / "mod_quality.py").is_file():
                _spawn_py("mod_quality.py")
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

    def _handle_log(self) -> None:
        try:
            cfg = _load_config_mem()
            log_path = f"{INSTALL_DIR}/logs/sentinel.log"
            log_data = "日志文件不存在或为空"
            if os.path.isfile(log_path):
                with open(log_path, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                if lines:
                    log_data = html.escape("".join(lines[-15:]))

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
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                cfg.get("TG_API_URL", ""),
                data=data,
                headers={
                    "User-Agent": f"IP-Sentinel-Agent/{ver}",
                    "Content-Type": "application/json",
                },
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"Log transmission failed: {exc}")

    def _handle_rename(self, query: dict) -> None:
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

            self._ok(b"Action Accepted: trigger_rename\n")
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Error: {exc}\n".encode())

    def _handle_toggle(self, query: dict) -> None:
        mod_name = query.get("mod", [""])[0]
        target_state = query.get("state", [""])[0].lower()
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
            self._ok(b"Action Accepted: trigger_toggle\n")
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Error: {exc}\n".encode())

    def _handle_ota(self) -> None:
        try:
            cfg = _load_config_mem()
            if cfg.get("ENABLE_OTA", "false").lower() != "true":
                self._forbidden(b"403 Forbidden: OTA Upgrade Disabled locally\n")
                return
            if cfg.get("TG_TOKEN") == "OFFICIAL_GATEWAY_MODE":
                self._forbidden(b"403 Forbidden: OTA strictly disabled under Public Gateway mode\n")
                return

            self._ok(b"Action Accepted: trigger_ota\n")

            repo_url = "https://raw.githubusercontent.com/hotyue/IP-Sentinel/main"
            install_sh = f"{INSTALL_DIR}/core/install.sh"
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
    bash /tmp/ota_agent.sh > {INSTALL_DIR}/logs/ota_upgrade.log 2>&1
else
    MSG=$(echo '{err_b64}' | base64 -d)
    curl -s -m 10 -X POST "{tg_url}" -d "chat_id={chat_id}" -d "text=$MSG" -d "parse_mode=Markdown" > /dev/null 2>&1
    echo "OTA Checksum Failed: Script corrupted" > {INSTALL_DIR}/logs/ota_upgrade.log
fi
"""
            ota_b64 = base64.b64encode(ota_script.encode()).decode()
            if shutil.which("systemd-run"):
                cmd = f"systemd-run --quiet --no-block bash -c \"echo '{ota_b64}' | base64 -d | bash\""
            else:
                cmd = f"nohup bash -c \"echo '{ota_b64}' | base64 -d | bash\" >/dev/null 2>&1 &"
            subprocess.Popen(cmd, shell=True, start_new_session=True)
        except Exception as exc:
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
