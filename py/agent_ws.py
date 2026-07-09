"""Agent WebSocket 客户端：通过 TG 确认 Master 公网后连接 WSS."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import socket
import ssl
import threading
import time
from typing import Any

from agent_commands import execute_agent_command
from config import load_config
from log_util import log
from master_public_ip import resolve_public_ip
from master_wss_resolve import request_master_wss_via_tg
from ws_protocol import dumps, loads, ws_sign

MODULE = "WSClient"
_RECONNECT_SEC = 5
_HEARTBEAT_SEC = 30

_hub_lock = threading.Lock()
_pending_trend: list[dict[str, Any]] = []


def queue_trend_save(
    node: str,
    score: str,
    goog: str,
    play: str,
    gemini: str,
) -> None:
    with _hub_lock:
        _pending_trend.append(
            {"node": node, "score": score, "goog": goog, "play": play, "gemini": gemini}
        )


def _node_name(cfg: dict[str, Any]) -> str:
    if cfg.get("NODE_NAME"):
        return str(cfg["NODE_NAME"])
    raw_ip = cfg.get("PUBLIC_IP", "127.0.0.1")
    ip_hash = hashlib.md5(str(raw_ip).encode()).hexdigest()[:4].upper()
    host = re.sub(r"[^a-zA-Z0-9]", "", socket.gethostname())[:10]
    return f"{host}-{ip_hash}"


def _wss_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class AgentWSClient:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._force_lookup = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="agent-wss")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            cfg = load_config()
            if not cfg:
                self._stop.wait(_RECONNECT_SEC)
                continue
            chat_id = (cfg.get("CHAT_ID") or "").strip()
            if not chat_id:
                log(cfg, MODULE, "WARN ", "未配置 CHAT_ID，WSS 客户端待机")
                self._stop.wait(30)
                continue

            node = _node_name(cfg)
            url = request_master_wss_via_tg(cfg, node, force=self._force_lookup)
            self._force_lookup = False
            if not url:
                self._stop.wait(30)
                continue

            try:
                asyncio.run(self._session(cfg, url, chat_id))
            except Exception as exc:
                log(cfg, MODULE, "WARN ", f"WSS 会话异常: {exc}，将重新通过 TG 确认 Master 公网")
                self._force_lookup = True
            if not self._stop.is_set():
                self._stop.wait(_RECONNECT_SEC)

    async def _session(self, cfg: dict[str, Any], url: str, chat_id: str) -> None:
        import websockets

        node = _node_name(cfg)
        ip = resolve_public_ip(cfg)
        ts = int(time.time())
        sign = ws_sign(chat_id, f"auth:{chat_id}:{node}:{ts}")

        async with websockets.connect(
            url,
            ssl=_wss_ssl_context(),
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            await ws.send(
                dumps(
                    {
                        "op": "auth",
                        "chat_id": chat_id,
                        "node": node,
                        "ts": ts,
                        "sign": sign,
                        "region": cfg.get("REGION_CODE", "UNKNOWN"),
                        "ip": ip,
                        "alias": cfg.get("NODE_ALIAS") or node,
                        "ota": cfg.get("ENABLE_OTA", "false"),
                    }
                )
            )
            auth_resp = loads(await ws.recv())
            if auth_resp.get("op") != "auth_ok":
                raise RuntimeError(auth_resp.get("error", "auth failed"))

            log(cfg, MODULE, "INFO ", f"已连接 Master WSS ({url})")
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                while not self._stop.is_set():
                    await self._flush_trend(ws)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = loads(raw)
                    if msg.get("op") == "cmd":
                        req_id = str(msg.get("id", ""))
                        path = str(msg.get("path", ""))
                        params = msg.get("params") or {}
                        status, body = await asyncio.to_thread(
                            execute_agent_command, path, params
                        )
                        await ws.send(
                            dumps(
                                {
                                    "op": "cmd_result",
                                    "id": req_id,
                                    "status": status,
                                    "body": body,
                                }
                            )
                        )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_SEC)
            await ws.send(dumps({"op": "ping"}))

    async def _flush_trend(self, ws: Any) -> None:
        batch: list[dict[str, Any]] = []
        with _hub_lock:
            if _pending_trend:
                batch = _pending_trend[:]
                _pending_trend.clear()
        for item in batch:
            await ws.send(dumps({"op": "trend_save", **item}))


def start_ws_client() -> AgentWSClient:
    client = AgentWSClient()
    client.start()
    return client
