"""Telegram Bot API (urllib + json)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self._ssl = ssl._create_unverified_context()

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._ssl) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return {}

    def send_message(self, chat_id: str, text: str, *, markdown: bool = True) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        self._post("sendMessage", payload)

    def send_ui(self, chat_id: str, text: str, keyboard: list) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
            },
        )

    def edit_ui(self, chat_id: str, message_id: int, text: str, keyboard: list) -> None:
        self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def answer_callback(self, callback_id: str, text: str, *, alert: bool = False) -> None:
        self._post(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": alert,
            },
        )

    def edit_reply_markup(self, chat_id: str, message_id: int, keyboard: list) -> None:
        self._post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def force_reply_rename(self, chat_id: str, node_name: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✏️ 请回复本消息以重命名节点:\n`{node_name}`\n"
                    "(仅限中英文、数字，最长20字符)"
                ),
                "parse_mode": "Markdown",
                "reply_markup": {"force_reply": True},
            },
        )

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        url = f"{self.base}/getUpdates?offset={offset}&timeout={timeout}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5, context=self._ssl) as resp:
                body = json.loads(resp.read().decode())
                return body.get("result", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return []
