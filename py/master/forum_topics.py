"""论坛话题本地注册表（Bot API 无法 list 已有话题，需自行积累）."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SEP_RE = re.compile(r"[·•|/\\\-—–]+")


def normalize_topic_name(text: str) -> str:
    """归一化话题标题便于模糊匹配."""
    s = (text or "").strip().lower()
    s = _SEP_RE.sub(" ", s)
    return " ".join(s.split())


class ForumTopicRegistry:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, dict[str, str]]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
            else:
                self._data = {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def set_topic(self, chat_id: str, thread_id: int, name: str) -> None:
        chat_key = str(chat_id)
        tid = str(int(thread_id))
        name = (name or "").strip()
        if not chat_key or not tid or not name:
            return
        bucket = self._data.setdefault(chat_key, {})
        bucket[tid] = {
            "name": name,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        self.save()

    def remove_topic(self, chat_id: str, thread_id: int) -> None:
        chat_key = str(chat_id)
        tid = str(int(thread_id))
        bucket = self._data.get(chat_key) or {}
        if tid in bucket:
            del bucket[tid]
            self.save()

    def by_id(self, chat_id: str) -> dict[int, str]:
        bucket = self._data.get(str(chat_id)) or {}
        out: dict[int, str] = {}
        for tid, meta in bucket.items():
            try:
                name = str((meta or {}).get("name") or "").strip()
                if name:
                    out[int(tid)] = name
            except ValueError:
                continue
        return out

    def by_name(self, chat_id: str) -> dict[str, int]:
        """标题 → thread_id（同名保留最新 updated）."""
        bucket = self._data.get(str(chat_id)) or {}
        ranked: list[tuple[str, int, str]] = []
        for tid, meta in bucket.items():
            name = str((meta or {}).get("name") or "").strip()
            if not name:
                continue
            try:
                ranked.append((name, int(tid), str((meta or {}).get("updated") or "")))
            except ValueError:
                continue
        ranked.sort(key=lambda x: x[2])
        out: dict[str, int] = {}
        for name, tid, _ in ranked:
            out[name] = tid
        return out

    def seed_from_nodes(
        self,
        rows: list[dict[str, Any]],
        *,
        title_fn,
    ) -> None:
        """用 DB 已有 binding 回填注册表."""
        changed = False
        for row in rows:
            thread_raw = row.get("message_thread_id")
            if not thread_raw:
                continue
            try:
                tid = int(thread_raw)
            except (TypeError, ValueError):
                continue
            alias = str(row.get("alias") or row.get("node_name") or "").strip()
            region = str(row.get("region") or "UNKNOWN").strip()
            node = str(row.get("node_name") or "").strip()
            if not alias and not node:
                continue
            name = title_fn(alias or node, region)
            chat_key = str(row.get("forum_chat_id") or "")
            if not chat_key:
                continue
            bucket = self._data.setdefault(chat_key, {})
            tid_key = str(tid)
            if bucket.get(tid_key, {}).get("name") != name:
                bucket[tid_key] = {
                    "name": name,
                    "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                }
                changed = True
        if changed:
            self.save()
