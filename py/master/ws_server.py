"""Master WebSocket 服务：Agent 主动连接，接收指令与趋势入库."""

from __future__ import annotations

import asyncio
import ssl
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from master.db import MasterDB
from master.security import sanitize_node_name, sanitize_score, sanitize_status_field
from wss_constants import MASTER_WSS_BIND, MASTER_WSS_PORT, build_master_wss_url
from ws_protocol import dumps, loads, verify_ws_auth

_CMD_TIMEOUT = 15.0


def ensure_master_tls_certs(master_dir: str) -> tuple[str, str]:
    """确保 Master WSS 自签证书存在."""
    core = Path(master_dir) / "core"
    core.mkdir(parents=True, exist_ok=True)
    cert = core / "ws_cert.pem"
    key = core / "ws_key.pem"
    if cert.is_file() and key.is_file():
        return str(cert), str(key)
    print("[master-ws] 正在生成 WSS 自签证书 (2048位 RSA)...", flush=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-days",
            "3650",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/C=US/O=IP-Sentinel/CN=Master-WSS",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return str(cert), str(key)


class AgentWSHub:
    """线程安全的 WSS 连接池与指令下发."""

    def __init__(self, db: MasterDB, *, master_dir: str) -> None:
        self.db = db
        self.host = MASTER_WSS_BIND
        self.port = MASTER_WSS_PORT
        self.master_dir = master_dir
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conns: dict[tuple[str, str], Any] = {}
        self._pending: dict[str, asyncio.Future[tuple[int, str]]] = {}
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="master-wss")
        self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    def _ssl_context(self) -> ssl.SSLContext:
        cert, key = ensure_master_tls_certs(self.master_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    async def _serve(self) -> None:
        import websockets

        ssl_ctx = self._ssl_context()
        async with websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ssl=ssl_ctx,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 ** 20,
        ):
            print(f"[master-wss] 监听 wss://{self.host}:{self.port}", flush=True)
            await asyncio.Future()
    async def _handle_client(self, ws: Any) -> None:
        key: tuple[str, str] | None = None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = loads(raw)
            if msg.get("op") != "auth":
                await ws.send(dumps({"op": "auth_err", "error": "auth required"}))
                return

            chat_id = str(msg.get("chat_id", ""))
            node = sanitize_node_name(str(msg.get("node", "")))
            ts = int(msg.get("ts", 0))
            sign = str(msg.get("sign", ""))
            if not chat_id or not node or not verify_ws_auth(chat_id, chat_id, node, ts, sign):
                await ws.send(dumps({"op": "auth_err", "error": "invalid signature"}))
                return

            key = (chat_id, node)
            async with self._lock:
                old = self._conns.get(key)
                if old is not None:
                    await old.close()
                self._conns[key] = ws

            region = str(msg.get("region", "UNKNOWN"))[:16]
            ip = str(msg.get("ip", ""))[:64]
            alias = str(msg.get("alias", node))[:30]
            ota = str(msg.get("ota", "false")).lower()
            self.db.execute(
                """INSERT INTO nodes (chat_id, node_name, agent_ip, last_seen,
                                      region, node_alias, enable_ota)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                   ON CONFLICT(chat_id, node_name) DO UPDATE SET
                     agent_ip=excluded.agent_ip,
                     last_seen=CURRENT_TIMESTAMP, region=excluded.region,
                     node_alias=excluded.node_alias, enable_ota=excluded.enable_ota""",
                (chat_id, node, ip, region, alias, ota),
            )

            await ws.send(dumps({"op": "auth_ok", "node": node}))
            print(f"[master-wss] Agent 已连接: {node} ({chat_id})", flush=True)

            async for raw in ws:
                await self._on_message(ws, key, raw)
        except Exception as exc:
            print(f"[master-wss] 连接异常: {exc}", flush=True)
        finally:
            if key:
                async with self._lock:
                    if self._conns.get(key) is ws:
                        del self._conns[key]

    async def _on_message(self, ws: Any, key: tuple[str, str], raw: str) -> None:
        try:
            msg = loads(raw)
        except ValueError:
            return
        op = msg.get("op")

        if op == "ping":
            await ws.send(dumps({"op": "pong"}))
            return

        if op == "cmd_result":
            req_id = str(msg.get("id", ""))
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                status = int(msg.get("status", 500))
                body = str(msg.get("body", ""))
                fut.set_result((status, body))
            return

        if op == "trend_save":
            node = sanitize_node_name(str(msg.get("node", key[1])))
            score = sanitize_score(str(msg.get("score", "")))
            goog = sanitize_status_field(str(msg.get("goog", "")))
            play = sanitize_status_field(str(msg.get("play", "")))
            gemini = sanitize_status_field(str(msg.get("gemini", "")))
            if node and score:
                self.db.execute(
                    """INSERT INTO ip_trend_log (node_name, scam_score, goog_status, nf_status, gpt_status)
                       VALUES (?, ?, ?, ?, ?)""",
                    (node, int(score), goog, play, gemini),
                )
            return

    def send_command(
        self,
        owner: str,
        node: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = _CMD_TIMEOUT,
    ) -> str:
        if not self._loop:
            return "FAILED"
        coro = self._send_command_async(owner, node, path, params or {}, timeout)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout + 3)
        except Exception:
            return "FAILED"

    async def _send_command_async(
        self,
        owner: str,
        node: str,
        path: str,
        params: dict[str, Any],
        timeout: float,
    ) -> str:
        key = (owner, sanitize_node_name(node))
        async with self._lock:
            ws = self._conns.get(key)
        if ws is None:
            return "FAILED"

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        result_fut: asyncio.Future[tuple[int, str]] = loop.create_future()
        self._pending[req_id] = result_fut

        try:
            await ws.send(
                dumps({"op": "cmd", "id": req_id, "path": path, "params": params})
            )
            status, body = await asyncio.wait_for(result_fut, timeout=timeout)
            if status >= 400:
                return body.strip() or f"ERROR {status}"
            return body
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return "FAILED"
        except Exception:
            self._pending.pop(req_id, None)
            return "FAILED"

    def is_online(self, owner: str, node: str) -> bool:
        key = (owner, sanitize_node_name(node))
        return key in self._conns
