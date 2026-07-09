"""向 Agent 下发 WebSocket 指令."""

from __future__ import annotations

from typing import Any

from master.ws_server import AgentWSHub

_hub: AgentWSHub | None = None


def bind_ws_hub(hub: AgentWSHub) -> None:
    global _hub
    _hub = hub


def call_agent(
    owner: str,
    node: str,
    path: str,
    params: dict[str, Any] | None = None,
    timeout: int = 15,
) -> str:
    if not _hub:
        return "FAILED"
    return _hub.send_command(owner, node, path, params, timeout=float(timeout))


def agent_online(owner: str, node: str) -> bool:
    if not _hub:
        return False
    return _hub.is_online(owner, node)
