"""Master ↔ Agent WebSocket 消息协议与 HMAC 鉴权."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def ws_sign(auth_key: str, message: str) -> str:
    return hmac.new(auth_key.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_ws_auth(auth_key: str, chat_id: str, node: str, ts: int, sign: str) -> bool:
    try:
        if abs(int(time.time()) - int(ts)) > 120:
            return False
    except (TypeError, ValueError):
        return False
    expected = ws_sign(auth_key, f"auth:{chat_id}:{node}:{ts}")
    return hmac.compare_digest(expected, sign)


def dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("message must be object")
    return data
