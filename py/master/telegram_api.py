"""Telegram Bot API (urllib + json)."""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

# Telegram legacy Markdown 中需转义的字符（代码块内除外）
_MD_SPECIAL = re.compile(r"([_*\[\]`])")


def escape_markdown(text: str) -> str:
    """转义动态文本，降低 parse_mode=Markdown 失败概率。"""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self._ssl = ssl._create_unverified_context()

    def _log_api_error(self, method: str, payload: dict[str, Any], body: dict[str, Any]) -> None:
        desc = body.get("description", body)
        print(f"[ip-sentinel-master] Telegram {method} failed: {desc}", file=sys.stderr, flush=True)

    @staticmethod
    def _with_thread(payload: dict[str, Any], message_thread_id: int | None) -> dict[str, Any]:
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        return payload

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=self._ssl) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[ip-sentinel-master] Telegram {method} network error: {exc}", file=sys.stderr, flush=True)
            return {"ok": False}

        if not body.get("ok"):
            desc = str(body.get("description", "")).lower()
            if payload.get("parse_mode") and (
                "can't parse" in desc or "parse entities" in desc or "can't find end" in desc
            ):
                plain = {k: v for k, v in payload.items() if k != "parse_mode"}
                return self._post(method, plain)
            if method == "editMessageText" and "message is not modified" in desc:
                return {"ok": True}
            if method == "deleteMessage" and "message to delete not found" in desc:
                return {"ok": True}
            self._log_api_error(method, payload, body)
        return body

    def get_me_username(self) -> str:
        body = self._post("getMe", {})
        return str((body.get("result") or {}).get("username") or "")

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        markdown: bool = True,
        message_thread_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        self._with_thread(payload, message_thread_id)
        body = self._post("sendMessage", payload)
        if not body.get("ok"):
            return None
        return (body.get("result") or {}).get("message_id")

    def send_ui(
        self,
        chat_id: str,
        text: str,
        keyboard: list,
        *,
        message_thread_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": keyboard},
        }
        self._with_thread(payload, message_thread_id)
        body = self._post("sendMessage", payload)
        if not body.get("ok"):
            return None
        return (body.get("result") or {}).get("message_id")

    def replace_message(
        self,
        chat_id: str,
        old_message_id: int | None,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> int | None:
        """先发新消息，成功后再删旧消息，返回新 message_id。发送失败时旧消息保留。"""
        new_id = self.send_message(chat_id, text, message_thread_id=message_thread_id)
        if new_id and old_message_id:
            self.delete_message(chat_id, old_message_id, message_thread_id=message_thread_id)
        return new_id

    def replace_ui(
        self,
        chat_id: str,
        old_message_id: int | None,
        text: str,
        keyboard: list,
        *,
        message_thread_id: int | None = None,
    ) -> int | None:
        """先发新 UI 消息，成功后再删旧消息，返回新 message_id。发送失败时旧消息保留。"""
        new_id = self.send_ui(chat_id, text, keyboard, message_thread_id=message_thread_id)
        if new_id and old_message_id:
            self.delete_message(chat_id, old_message_id, message_thread_id=message_thread_id)
        return new_id

    def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        new_id = self.replace_message(
            chat_id, message_id, text, message_thread_id=message_thread_id
        )
        return new_id is not None

    def edit_ui(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        keyboard: list,
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        new_id = self.replace_ui(
            chat_id, message_id, text, keyboard, message_thread_id=message_thread_id
        )
        return new_id is not None

    def delete_message(
        self,
        chat_id: str,
        message_id: int,
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        self._with_thread(payload, message_thread_id)
        return bool(self._post("deleteMessage", payload).get("ok"))

    def create_forum_topic(self, chat_id: str, name: str) -> int | None:
        body = self._post(
            "createForumTopic",
            {"chat_id": chat_id, "name": name[:128]},
        )
        if not body.get("ok"):
            return None
        thread = (body.get("result") or {}).get("message_thread_id")
        return int(thread) if thread else None

    def get_forum_topics(self, chat_id: str) -> list[dict[str, Any]]:
        """拉取群组当前全部 Forum 话题（分页）."""
        topics: list[dict[str, Any]] = []
        offset_topic = 0
        while True:
            payload: dict[str, Any] = {"chat_id": chat_id, "limit": 100}
            if offset_topic:
                payload["offset_topic"] = offset_topic
            body = self._post("getForumTopics", payload)
            if not body.get("ok"):
                break
            batch = (body.get("result") or {}).get("topics") or []
            if not batch:
                break
            topics.extend(batch)
            if len(batch) < 100:
                break
            offset_topic = int(batch[-1].get("message_thread_id") or 0)
            if not offset_topic:
                break
        return topics

    def answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_id,
            "show_alert": alert,
        }
        if text:
            payload["text"] = text[:200]
        self._post("answerCallbackQuery", payload)

    def edit_reply_markup(
        self,
        chat_id: str,
        message_id: int,
        keyboard: list,
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        self._with_thread(payload, message_thread_id)
        return bool(self._post("editMessageReplyMarkup", payload).get("ok"))

    def force_reply_rename(
        self,
        chat_id: str,
        node_name: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": (
                f"✏️ 请回复本消息以重命名节点:\n`{escape_markdown(node_name)}`\n"
                "(仅限中英文、数字，最长20字符)"
            ),
            "parse_mode": "Markdown",
            "reply_markup": {"force_reply": True},
        }
        self._with_thread(payload, message_thread_id)
        self._post("sendMessage", payload)

    def force_reply_prompt(
        self,
        chat_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": {"force_reply": True},
        }
        self._with_thread(payload, message_thread_id)
        self._post("sendMessage", payload)

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        url = f"{self.base}/getUpdates?offset={offset}&timeout={timeout}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout + 10, context=self._ssl) as resp:
                body = json.loads(resp.read().decode())
                if not body.get("ok"):
                    self._log_api_error("getUpdates", {}, body)
                    return []
                return body.get("result", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[ip-sentinel-master] getUpdates error: {exc}", file=sys.stderr, flush=True)
            return []

    def set_my_description(self, description: str) -> bool:
        body = self._post("setMyDescription", {"description": description[:512]})
        return bool(body.get("ok"))
