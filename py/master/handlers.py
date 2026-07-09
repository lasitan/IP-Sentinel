#!/usr/bin/env python3
"""Master Telegram 指令路由与业务处理."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from master.agent_client import call_agent
from master.config import save_master_config_keys
from master.db import MasterDB
from master.flags import get_flag
from master_public_ip import normalize_ip_for_storage, resolve_public_ip
from master.security import (
    alias_to_b64,
    is_ssrf_ip,
    sanitize_agent_ip,
    sanitize_alias,
    sanitize_chat_id,
    sanitize_node_name,
    sanitize_region,
    sanitize_score,
    sanitize_status_field,
)
from master.telegram_api import TelegramAPI
from wss_constants import build_master_wss_url

REPO_RAW_URL = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"

@dataclass
class _TgCtx:
    """Telegram 会话上下文：owner 用于 DB/HMAC，chat/thread 用于 UI 路由."""

    owner: str
    chat: str
    thread: int | None
    msg_id: int | None
    forum_mode: bool
    forum_chat_id: str

    @property
    def in_forum(self) -> bool:
        return self.forum_mode and self.chat == self.forum_chat_id


class MasterHandlers:
    def __init__(self, cfg: dict[str, Any], db: MasterDB, tg: TelegramAPI) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = tg
        self.version = cfg.get("MASTER_VERSION", "4.1.1")
        self.official = cfg.get("IS_OFFICIAL_GATEWAY", "false").lower() == "true"
        self.master_ota = cfg.get("ENABLE_MASTER_OTA", "false").lower() == "true"
        self._ctx = _TgCtx("", "", None, None, False, "")
        self._bot_username = (tg.get_me_username() or "").lower()
        self._pending_rename: dict[int, str] = {}
        self._master_public_ip = normalize_ip_for_storage(resolve_public_ip(self.cfg))
        self._sync_master_wss_description()

    def _refresh_master_public_ip(self) -> str:
        ip = resolve_public_ip(self.cfg)
        if ip:
            self._master_public_ip = normalize_ip_for_storage(ip)
        return self._master_public_ip

    def _master_wss_reply(self) -> str:
        ip = self._refresh_master_public_ip()
        if not ip:
            return ""
        return f"#MASTER_WSS#|{ip}"

    def _sync_master_wss_description(self) -> None:
        """将 Master 公网 IP 写入 Bot Description，供 Agent 通过 getMyDescription 读取."""
        ip = self._refresh_master_public_ip()
        if not ip:
            return
        wss = build_master_wss_url(ip)
        desc = f"IP-Sentinel Master WSS\n#MASTER_WSS#|{ip}\n{wss}"
        self.tg.set_my_description(desc)

    def _send_master_wss_reply(self, chat_id: str) -> None:
        self._sync_master_wss_description()
        line = self._master_wss_reply()
        if line:
            wss = build_master_wss_url(line.split("|", 1)[1])
            self.tg.send_message(
                chat_id,
                f"{line}\n📡 Agent 请连接: `{wss}`",
                markdown=False,
            )
        else:
            self.tg.send_message(chat_id, "⚠️ Master 未能探测公网 IP，请检查出站网络。", markdown=False)

    def handle_wss_lookup(self, chat_id: str, text: str) -> bool:
        if not text.startswith("#WSS_LOOKUP#"):
            return False
        self._send_master_wss_reply(chat_id)
        return True

    def _normalize_cmd(self, text: str) -> str:
        """解析 /start@BotName 等群组内 @ 指令."""
        t = text.strip()
        if not t.startswith("/"):
            return t
        parts = t.split(maxsplit=1)
        head = parts[0]
        if "@" not in head:
            return t
        cmd, mention = head.split("@", 1)
        if self._bot_username and mention.lower() != self._bot_username:
            return t
        tail = parts[1] if len(parts) > 1 else ""
        return f"{cmd} {tail}".strip() if tail else cmd

    def _node_for_thread(self, thread_id: int | None) -> str | None:
        if not thread_id:
            return None
        row = self.db.execute(
            "SELECT node_name FROM nodes WHERE message_thread_id=? LIMIT 1",
            (thread_id,),
        )
        return row[0]["node_name"] if row else None

    def _get_topic_ui(self, owner: str, node: str) -> tuple[int | None, int]:
        row = self.db.execute(
            """SELECT topic_ui_message_id, COALESCE(topic_ui_edit_count, 0) AS c
               FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1""",
            (owner, node),
        )
        if not row:
            return None, 0
        mid = row[0]["topic_ui_message_id"]
        return (int(mid) if mid else None, int(row[0]["c"]))

    def _set_topic_ui(self, owner: str, node: str, msg_id: int | None, edit_count: int = 0) -> None:
        self.db.execute(
            """UPDATE nodes SET topic_ui_message_id=?, topic_ui_edit_count=?
               WHERE chat_id=? AND node_name=?""",
            (msg_id, edit_count, owner, node),
        )

    def _append_back_btn(self, keyboard: list, node: str, *, on_manage: bool = False) -> list:
        kb = [row[:] for row in keyboard]
        cb = f"manage:{node}"
        if any(btn.get("callback_data") == cb for row in kb for btn in row):
            return kb
        label = "🔄 刷新控制台" if on_manage else "⬅️ 返回控制台"
        kb.append([{"text": label, "callback_data": cb}])
        return kb

    def _topic_present(
        self,
        owner: str,
        node: str,
        text: str,
        keyboard: list,
        *,
        on_manage: bool = False,
    ) -> None:
        """话题内唯一 Master 消息：删旧后发新."""
        thread = self._node_thread_id(owner, node)
        if not thread or not self.forum_chat_id:
            return
        dest = self.forum_chat_id
        kb = self._append_back_btn(keyboard, node, on_manage=on_manage)
        ui_id, _ = self._get_topic_ui(owner, node)
        new_id = self.tg.replace_ui(dest, ui_id, text, kb, message_thread_id=thread)
        if new_id:
            self._set_topic_ui(owner, node, new_id, 0)
        self._sync_agent_topic_bot(owner, node)

    def _topic_title(self, alias: str, region: str) -> str:
        return f"{alias} · {region}"

    def _fetch_forum_topics_map(self) -> dict[str, int]:
        """Telegram 群组当前真实话题：标题 → message_thread_id."""
        if not self.forum_chat_id:
            return {}
        out: dict[str, int] = {}
        for topic in self.tg.get_forum_topics(self.forum_chat_id):
            name = str(topic.get("name") or "").strip()
            tid = topic.get("message_thread_id")
            if name and tid:
                out[name] = int(tid)
        return out

    def _resolve_node_thread_from_topics(
        self,
        alias: str,
        region: str,
        db_thread: int | None,
        live_topics: dict[str, int],
    ) -> int | None:
        """在 Telegram 真实话题列表中解析节点话题（优先标题，其次 DB thread_id）."""
        title = self._topic_title(alias, region)
        if title in live_topics:
            return live_topics[title]
        if db_thread and db_thread in live_topics.values():
            return db_thread
        return None

    def _count_nodes_missing_live_topic(self, owner: str) -> int:
        rows = self.db.execute(
            """SELECT COALESCE(node_alias, node_name) AS alias, region, message_thread_id
               FROM nodes WHERE chat_id=?""",
            (owner,),
        )
        if not rows:
            return 0
        live = self._fetch_forum_topics_map()
        missing = 0
        for row in rows:
            db_thread = int(row["message_thread_id"]) if row["message_thread_id"] else None
            if not self._resolve_node_thread_from_topics(
                row["alias"], row["region"] or "UNKNOWN", db_thread, live
            ):
                missing += 1
        return missing

    def _sync_agent_topic_bot(self, owner: str, node: str) -> None:
        auth = self._auth_key(owner)
        threading.Thread(
            target=self._push_topic_to_agent,
            args=(owner, node, auth),
            daemon=True,
        ).start()

    def _is_node_topic(self) -> bool:
        return bool(
            self.forum_mode
            and self._ctx.chat == self.forum_chat_id
            and self._ctx.thread
            and self._node_for_thread(self._ctx.thread)
        )

    def _forum_general_edit(
        self,
        text: str,
        keyboard: list,
        msg_id: int | None = None,
    ) -> None:
        """General：删旧 callback 消息后发新；节点话题仍走 _topic_present."""
        dest = self.forum_chat_id
        thread = self._ctx.thread
        mid = msg_id or self._ctx.msg_id
        if mid:
            self.tg.replace_ui(dest, mid, text, keyboard, message_thread_id=thread)
        else:
            self.tg.send_ui(dest, text, keyboard, message_thread_id=thread)

    def _forum_node_active(self, owner: str, node: str) -> bool:
        """节点已绑定论坛话题时，UI/反馈应走话题而非私聊."""
        return bool(self.forum_mode and self._node_thread_id(owner, node))

    def _show_global_menu(
        self, text: str, keyboard: list, msg_id: int | None = None
    ) -> None:
        """全局菜单（主菜单/区域列表）：论坛模式下统一展示在群组 General。"""
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(text, keyboard, msg_id)
            return
        if self.forum_mode and self.forum_chat_id:
            dest, thread = self._menu_dest()
            if msg_id and self._ctx.chat == dest:
                self.tg.edit_ui(dest, msg_id, text, keyboard, message_thread_id=thread)
            else:
                self.tg.send_ui(dest, text, keyboard, message_thread_id=thread)
            return
        if msg_id and self._ctx.chat == self._ctx.owner:
            self.tg.edit_ui(self._ctx.owner, msg_id, text, keyboard)
        else:
            self.tg.send_ui(self._ctx.owner, text, keyboard)

    def _notify_owner(self, text: str, *, markdown: bool = True) -> None:
        """非节点专属提示：论坛模式进群组 General，否则私聊 owner。"""
        if self.forum_mode and self.forum_chat_id:
            if self._ctx.chat == self.forum_chat_id:
                dest, thread = self._reply_chat()
            else:
                dest, thread = self._menu_dest()
            self.tg.send_message(dest, text, markdown=markdown, message_thread_id=thread)
            return
        self.tg.send_message(self._ctx.owner, text, markdown=markdown)

    def _forum_menu(
        self,
        text: str,
        keyboard: list,
        msg_id: int | None = None,
        *,
        on_manage: bool = False,
    ) -> None:
        """群聊菜单：节点话题走节点单消息，General 仅编辑当前消息."""
        if self._is_node_topic():
            node = self._node_for_thread(self._ctx.thread)
            assert node
            self._topic_present(
                self._ctx.owner, node, text, keyboard, on_manage=on_manage
            )
            return
        if self._ctx.chat == self.forum_chat_id:
            self._forum_general_edit(text, keyboard, msg_id)
            return
        if self.forum_mode and self.forum_chat_id:
            self._show_global_menu(text, keyboard, msg_id)
            return
        if msg_id:
            self.tg.edit_ui(self._ctx.owner, msg_id, text, keyboard)
        else:
            self.tg.send_ui(self._ctx.owner, text, keyboard)

    def _delete_user_msg(self, user_msg_id: int | None) -> None:
        if not user_msg_id or not self._ctx.in_forum:
            return
        self.tg.delete_message(
            self._ctx.chat, user_msg_id, message_thread_id=self._ctx.thread
        )

    def _in_topic_flow(self, owner: str, node: str) -> bool:
        return self._forum_node_active(owner, node)

    @property
    def forum_mode(self) -> bool:
        return (
            self.cfg.get("FORUM_MODE", "false").lower() == "true"
            and bool(sanitize_chat_id(str(self.cfg.get("FORUM_CHAT_ID", ""))))
        )

    @property
    def forum_chat_id(self) -> str:
        return sanitize_chat_id(str(self.cfg.get("FORUM_CHAT_ID", "")))

    def _node_from_callback(self, text: str) -> str | None:
        if not text or ":" not in text:
            return None
        head = text.split(":")[0]
        if head == "toggle" and text.count(":") >= 3:
            return sanitize_node_name(text.split(":")[2])
        if head in (
            "manage", "google", "trust", "run", "report", "log", "log_refresh",
            "quality", "trend", "ota_confirm", "ota_execute", "rename", "del",
        ):
            return sanitize_node_name(text.split(":", 1)[1])
        return None

    def _forum_owner(self) -> str:
        """话题群组对应的节点 owner（私聊 chat_id）."""
        saved = sanitize_chat_id(str(self.cfg.get("FORUM_OWNER_CHAT_ID", "")))
        if saved:
            return saved
        rows = self.db.execute(
            """SELECT chat_id FROM nodes
               WHERE message_thread_id IS NOT NULL AND message_thread_id != 0
               GROUP BY chat_id
               ORDER BY COUNT(*) DESC
               LIMIT 1"""
        )
        if rows:
            owner = rows[0]["chat_id"]
        else:
            row = self.db.execute("SELECT chat_id FROM nodes LIMIT 1")
            if not row:
                return ""
            owner = row[0]["chat_id"]
        save_master_config_keys({"FORUM_OWNER_CHAT_ID": owner})
        self.cfg["FORUM_OWNER_CHAT_ID"] = owner
        return owner

    def _reply_chat(self) -> tuple[str, int | None]:
        """Telegram 回复目标：群聊内操作回到群组，其余回到 owner 私聊."""
        if self.forum_mode and self._ctx.chat == self.forum_chat_id:
            return self._ctx.chat, self._ctx.thread
        return self._ctx.owner, None

    def _menu_dest(self) -> tuple[str, int | None]:
        """非节点专属菜单（区域列表等）的展示目标."""
        if self.forum_mode and self.forum_chat_id:
            if self._ctx.chat == self.forum_chat_id:
                return self.forum_chat_id, self._ctx.thread
            return self.forum_chat_id, None
        return self._ctx.owner, None

    def _resolve_owner(self, chat_id: str, thread_id: int | None, text: str) -> str:
        if not self.forum_mode or chat_id != self.forum_chat_id:
            return chat_id
        if thread_id:
            row = self.db.execute(
                "SELECT chat_id FROM nodes WHERE message_thread_id=? LIMIT 1",
                (thread_id,),
            )
            if row:
                return row[0]["chat_id"]
        node = self._node_from_callback(text)
        if node:
            row = self.db.execute(
                "SELECT chat_id FROM nodes WHERE node_name=? LIMIT 1",
                (node,),
            )
            if row:
                return row[0]["chat_id"]
        owner = self._forum_owner()
        return owner if owner else chat_id

    def _node_thread_id(self, owner_chat_id: str, node: str) -> int | None:
        row = self.db.execute(
            "SELECT message_thread_id FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1",
            (owner_chat_id, node),
        )
        if not row or not row[0]["message_thread_id"]:
            return None
        return int(row[0]["message_thread_id"])

    def _node_tg_dest(self, owner_chat_id: str, node: str) -> tuple[str, int | None]:
        thread = self._node_thread_id(owner_chat_id, node)
        if self.forum_mode and thread:
            return self.forum_chat_id, thread
        if self._ctx.in_forum:
            return self._ctx.chat, self._ctx.thread
        return owner_chat_id, None

    def _can_edit_in_place(self, dest_chat: str, dest_thread: int | None) -> bool:
        if not self._ctx.msg_id:
            return False
        if self._ctx.chat != dest_chat:
            return False
        if dest_thread and self._ctx.thread != dest_thread:
            return False
        return True

    def _ui_node(
        self,
        owner_chat_id: str,
        node: str,
        text: str,
        keyboard: list,
        *,
        on_manage: bool = False,
    ) -> None:
        dest, thread = self._node_tg_dest(owner_chat_id, node)
        if self._in_topic_flow(owner_chat_id, node) or (
            dest == self.forum_chat_id and thread
        ):
            self._topic_present(owner_chat_id, node, text, keyboard, on_manage=on_manage)
            if (
                dest != owner_chat_id
                and not self._ctx.in_forum
                and not self.forum_mode
            ):
                alias = self.db.scalar(
                    "SELECT COALESCE(node_alias, node_name) FROM nodes WHERE chat_id=? AND node_name=?",
                    (owner_chat_id, node),
                ) or node
                self.tg.send_message(
                    owner_chat_id,
                    f"⚙️ 节点 `{alias}` 控制台已在群组话题中更新。",
                )
            return
        if self._can_edit_in_place(dest, thread):
            self.tg.replace_ui(
                dest, self._ctx.msg_id, text, keyboard, message_thread_id=thread
            )
            return
        self.tg.send_ui(dest, text, keyboard, message_thread_id=thread)

    def _msg_node(
        self,
        owner_chat_id: str,
        node: str,
        text: str,
        *,
        markdown: bool = True,
    ) -> None:
        kb = [[{"text": "⬅️ 返回控制台", "callback_data": f"manage:{node}"}]]
        dest, thread = self._node_tg_dest(owner_chat_id, node)
        if self._in_topic_flow(owner_chat_id, node) or (
            dest == self.forum_chat_id and thread
        ):
            self._topic_present(owner_chat_id, node, text, kb)
            return
        if self._can_edit_in_place(dest, thread):
            self.tg.replace_message(
                dest, self._ctx.msg_id, text, message_thread_id=thread
            )
            return
        self.tg.send_message(dest, text, markdown=markdown, message_thread_id=thread)

    def _push_topic_to_agent(
        self,
        owner_chat_id: str,
        node: str,
        auth: str,
        *,
        sync: bool = False,
    ) -> None:
        del auth
        thread = self._node_thread_id(owner_chat_id, node)
        if not thread:
            return
        ui_id, _ = self._get_topic_ui(owner_chat_id, node)
        params: dict[str, str] = {
            "dest_chat": self.forum_chat_id,
            "thread_id": str(thread),
        }
        if ui_id:
            params["bot_msg_id"] = str(ui_id)
        if sync:
            call_agent(owner_chat_id, node, "/trigger_set_topic", params)
        else:
            threading.Thread(
                target=call_agent,
                args=(owner_chat_id, node, "/trigger_set_topic", params),
                daemon=True,
            ).start()

    def _setup_node_topic(
        self,
        owner_chat_id: str,
        node: str,
        auth: str,
        *,
        alias: str | None = None,
        region: str | None = None,
        send_console: bool = True,
        live_topics: dict[str, int] | None = None,
    ) -> int | None:
        """创建/匹配节点话题：以 Telegram 当前话题列表为准，并回写 DB."""
        if not self.forum_mode or not self.forum_chat_id:
            return None
        if alias is None or region is None:
            row = self.db.execute(
                """SELECT COALESCE(node_alias, node_name) AS alias, region, message_thread_id
                   FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1""",
                (owner_chat_id, node),
            )
            if not row:
                return None
            alias = row[0]["alias"]
            region = row[0]["region"] or "UNKNOWN"
            db_thread = (
                int(row[0]["message_thread_id"]) if row[0]["message_thread_id"] else None
            )
        else:
            db_thread = self._node_thread_id(owner_chat_id, node)

        topics = live_topics if live_topics is not None else self._fetch_forum_topics_map()
        thread_id = self._resolve_node_thread_from_topics(
            alias, region or "UNKNOWN", db_thread, topics
        )

        if not thread_id:
            thread_id = self.tg.create_forum_topic(
                self.forum_chat_id, self._topic_title(alias, region or "UNKNOWN")
            )
            if not thread_id:
                return None

        if thread_id != db_thread:
            self.db.execute(
                "UPDATE nodes SET message_thread_id=? WHERE chat_id=? AND node_name=?",
                (thread_id, owner_chat_id, node),
            )

        if send_console:
            panel, kb = self._manage_keyboard(owner_chat_id, node, for_topic=True)
            if kb:
                self._topic_present(owner_chat_id, node, panel, kb, on_manage=True)
        self._push_topic_to_agent(owner_chat_id, node, auth)
        return thread_id

    def _auth_key(self, chat_id: str) -> str:
        """与 Agent 端 CHAT_ID 预共享密钥一致."""
        return sanitize_chat_id(chat_id)

    def _region_keyboard(self, chat_id: str, *, home_btn: bool = False) -> list:
        rows = self.db.execute(
            "SELECT region, COUNT(*) AS c FROM nodes WHERE chat_id=? GROUP BY region",
            (chat_id,),
        )
        if not rows:
            return []
        kb: list = []
        for row in rows:
            region = row["region"] or "UNKNOWN"
            flag = get_flag(region)
            kb.append([{"text": f"{flag} {region} ({row['c']} 台)", "callback_data": f"region:{region}"}])
        if home_btn:
            kb.append([{"text": "🏠 返回主菜单", "callback_data": "/start"}])
        return kb

    def _manage_keyboard(
        self, chat_id: str, node: str, *, for_topic: bool = False
    ) -> tuple[str, list]:
        row = self.db.execute(
            """SELECT enable_google, enable_trust, enable_ota, agent_ip,
                      COALESCE(last_seen, '未知') AS last_seen,
                      COALESCE(node_alias, node_name) AS alias
               FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1""",
            (chat_id, node),
        )
        if not row:
            return node, []
        r = row[0]
        st_g, st_t, st_ota = r["enable_google"], r["enable_trust"], r["enable_ota"]
        act_g = "false" if st_g == "true" else "true"
        act_t = "false" if st_t == "true" else "true"
        btn_g = "🟢 Google 模块: 开" if st_g == "true" else "🔴 Google 模块: 关"
        btn_t = "🟢 Trust 模块: 开" if st_t == "true" else "🔴 Trust 模块: 关"

        action = [
            [
                {"text": "📍 触发 Google 纠偏", "callback_data": f"google:{node}"},
                {"text": "🛡️ 触发信用净化", "callback_data": f"trust:{node}"},
            ],
            [
                {"text": "🔍 IP 质量检测", "callback_data": f"quality:{node}"},
                {"text": "📈 查看 IP 污染趋势图", "callback_data": f"trend:{node}"},
            ],
            [
                {"text": "📜 提取终端实时日志", "callback_data": f"log:{node}"},
                {"text": "📊 生成报告", "callback_data": f"report:{node}"},
            ],
        ]
        toggle = [
            [
                {"text": btn_g, "callback_data": f"toggle:google:{node}:{act_g}"},
                {"text": btn_t, "callback_data": f"toggle:trust:{node}:{act_t}"},
            ],
        ]
        if not self.official and st_ota == "true":
            config = [
                [
                    {"text": "✏️ 更改终端展示代号", "callback_data": f"rename:{node}"},
                    {"text": "🆙 OTA 静默升级", "callback_data": f"ota_confirm:{node}"},
                ],
            ]
        else:
            config = [[{"text": "✏️ 更改终端展示代号", "callback_data": f"rename:{node}"}]]
        danger = (
            [[{"text": "🗑️ 删除节点", "callback_data": f"del:{node}"}]]
            if for_topic
            else [
                [
                    {"text": "🗑️ 删除节点", "callback_data": f"del:{node}"},
                    {"text": "⬅️ 返回区域列表", "callback_data": "list_nodes"},
                ],
            ]
        )
        alias = r["alias"] or node
        text = (
            f"⚙️ **节点**: `{alias}`\n"
            f"(ID: `{node}`)\n"
            f"🌐 IP: `{r['agent_ip']}`\n"
            f"🕒 最后在线: `{r['last_seen']}`\n\n"
            "请选择操作："
        )
        return text, action + toggle + config + danger

    def _trend_text(self, chat_id: str, node: str) -> str:
        rows = self.db.execute(
            """SELECT datetime(check_time, 'localtime') AS t, scam_score,
                      goog_status, nf_status, gpt_status
               FROM ip_trend_log WHERE node_name=? ORDER BY check_time DESC LIMIT 15""",
            (node,),
        )
        if not rows:
            return f"⚠️ 节点 `{node}` 暂无历史记录。请先执行 IP 质量检测。"
        alias = self.db.scalar(
            "SELECT COALESCE(node_alias, node_name) FROM nodes WHERE chat_id=? AND node_name=?",
            (chat_id, node),
        ) or node
        lines = [
            f"📈 *[{alias}] 历史记录 (近 15 次)*\n",
            "时间(本地)  | 风险 | YT区域 | Play | Gemini",
            "-------------------------------------------",
        ]
        for row in rows:
            score = row["scam_score"] or 0
            goog = row["goog_status"] or "未知"
            nf = row["nf_status"] or "未知"
            gpt = row["gpt_status"] or "未知"
            short = (row["t"] or "")[5:16]
            if score <= 20:
                emj = "🟢"
            elif score <= 60:
                emj = "🟡"
            else:
                emj = "🔴"
            lines.append(f"`{short}` | {emj}`{score}` | `{goog}` | `{nf}` | `{gpt}`")
        lines.append("\n_💡 风险分 >60 可能触发验证码；Google 显示 CN 表示被判定为中国大陆。_")
        return "\n".join(lines)

    def _fanout_agents(
        self,
        chat_id: str,
        path: str,
        *,
        filter_ota: bool = False,
        delay: float = 0.0,  # 并发模式下不再使用，保留参数保持兼容
    ) -> None:
        sql = "SELECT node_name FROM nodes WHERE chat_id=?"
        params: tuple = (chat_id,)
        if filter_ota:
            sql += " AND enable_ota='true'"
        rows = self.db.execute(sql, params)
        auth = self._auth_key(chat_id)

        def _call_one(row: dict) -> None:
            call_agent(chat_id, row["node_name"], path)

        for row in rows:
            threading.Thread(target=_call_one, args=(row,), daemon=True).start()

    def _fanout_reports(self, chat_id: str) -> None:
        """全部报告：每节点独立线程并发下发，预写话题消息后立即触发 report."""
        rows = self.db.execute(
            "SELECT node_name FROM nodes WHERE chat_id=?",
            (chat_id,),
        )
        auth = self._auth_key(chat_id)

        def _call_one(row: dict) -> None:
            node = row["node_name"]
            if self.forum_mode and self._node_thread_id(chat_id, node):
                alias = (
                    self.db.scalar(
                        "SELECT COALESCE(node_alias, node_name) FROM nodes WHERE chat_id=? AND node_name=?",
                        (chat_id, node),
                    )
                    or node
                )
                kb = [[{"text": "⬅️ 返回控制台", "callback_data": f"manage:{node}"}]]
                self._topic_present(
                    chat_id,
                    node,
                    f"⏳ 正在生成 `{alias}` 报告…",
                    kb,
                )
                self._push_topic_to_agent(chat_id, node, auth, sync=True)
            call_agent(chat_id, node, "/trigger_report")

        for row in rows:
            threading.Thread(target=_call_one, args=(row,), daemon=True).start()

    def handle_svq(
        self,
        chat_id: str,
        text: str,
        cb_id: str | None,
        msg_id: int | None,
    ) -> bool:
        parts = text.split("|", 5)
        if len(parts) < 6:
            return False
        _, raw_node, raw_score, goog, nf, gpt = parts
        node = sanitize_node_name(raw_node)
        score = sanitize_score(raw_score)
        goog = sanitize_status_field(goog)
        nf = sanitize_status_field(nf)
        gpt = sanitize_status_field(gpt)
        if not node or not score:
            if cb_id:
                self.tg.answer_callback(cb_id, "❌ 数据解析失败，入库中止。", alert=True)
            return True
        self.db.execute(
            """INSERT INTO ip_trend_log (node_name, scam_score, goog_status, nf_status, gpt_status)
               VALUES (?, ?, ?, ?, ?)""",
            (node, int(score), goog, nf, gpt),
        )
        if cb_id:
            self.tg.answer_callback(cb_id, "✅ 报告已成功录入趋势库！")
        if msg_id:
            kb = [
                [{"text": "✅ 此报告已存档", "callback_data": "ignore"}],
                [{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node}"}],
            ]
            dest, thread = self._node_tg_dest(chat_id, node)
            self.tg.edit_reply_markup(dest, msg_id, kb, message_thread_id=thread)
        return True

    def handle_register(self, chat_id: str, text: str) -> bool:
        if "#REGISTER#" not in text:
            return False
        line = next((ln for ln in text.splitlines() if "#REGISTER#" in ln), text)
        line = line.replace("`", "").strip()
        fields = line.split("|")
        n = len(fields)
        if n >= 7:
            # 旧格式含 agent_port 字段，跳过
            _, region, node, ip, _port, alias, ota = fields[:7]
        elif n >= 6:
            _, region, node, ip, alias, ota = fields[:6]
        elif n == 5:
            _, region, node, ip, alias = fields[:5]
            ota = "false"
        else:
            _, node, ip, _port = fields[:4]
            region, alias, ota = "UNKNOWN", node, "false"

        region = sanitize_region(region)
        node = sanitize_node_name(node)
        ip = sanitize_agent_ip(ip)
        alias = sanitize_alias(alias) or node
        ota = re.sub(r"[^a-z]", "", (ota or "false").lower()) or "false"

        if is_ssrf_ip(ip):
            self.tg.send_message(chat_id, "⛔ **安全拦截**：禁止注册内网或回环 IP，防止 SSRF 攻击渗透。")
            return True
        if not node or not ip:
            self.tg.send_message(chat_id, "⛔ **安全拦截**：检测到非法注册载荷，请求已拒绝。")
            return True

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
        auth = self._auth_key(chat_id)
        topic_note = ""
        if self.forum_mode and self.forum_chat_id:
            thread_id = self._setup_node_topic(
                chat_id, node, auth, alias=alias, region=region
            )
            if thread_id:
                topic_note = f"\n📌 已在群组创建话题 `{alias}`，请在该话题内维护此节点。"
            else:
                topic_note = "\n⚠️ 话题创建失败：请确认 Bot 为群组管理员且已开启 Topics。"
        self.tg.send_message(
            chat_id,
            f"✅ **已注册 (v{self.version})**\n节点 `{alias}` 已加入列表。{topic_note}",
        )
        kb = self._region_keyboard(chat_id)
        if kb:
            menu_body = "🌍 **按区域查看节点**\n请选择区域："
            if self.forum_mode and self.forum_chat_id:
                self.tg.send_ui(self.forum_chat_id, menu_body, kb)
            else:
                self.tg.send_ui(chat_id, menu_body, kb)
        self._send_master_wss_reply(chat_id)
        return True

    def handle_rename_reply(self, text: str, reply_text: str) -> str | None:
        if "✏️ 请回复本消息以重命名节点:" not in reply_text:
            return None
        target = sanitize_node_name(
            reply_text.replace("✏️ 请回复本消息以重命名节点:", "").split("\n")[0].strip("` ")
        )
        new_alias = sanitize_alias(text.replace("_", "-"), 30)
        if target and new_alias:
            return f"do_rename:{target}:{new_alias}"
        return None

    def dispatch(
        self,
        chat_id: str,
        text: str,
        *,
        cb_id: str | None = None,
        msg_id: int | None = None,
        thread_id: int | None = None,
        user_msg_id: int | None = None,
        reply_to_text: str = "",
    ) -> None:
        incoming_chat = sanitize_chat_id(chat_id)
        if not incoming_chat:
            return
        text = self._normalize_cmd(text)
        owner_chat_id = self._resolve_owner(incoming_chat, thread_id, text)
        self._ctx = _TgCtx(
            owner=owner_chat_id,
            chat=incoming_chat,
            thread=thread_id,
            msg_id=msg_id,
            forum_mode=self.forum_mode,
            forum_chat_id=self.forum_chat_id,
        )
        chat_id = owner_chat_id
        user_msg_deleted = False

        if text.startswith("svq|"):
            if self.handle_svq(chat_id, text, cb_id, msg_id):
                return

        rewritten = self.handle_rename_reply(text, reply_to_text)
        if rewritten:
            text = rewritten
        else:
            forum_bind = self.handle_forum_bind_reply(text, reply_to_text)
            if forum_bind:
                text = forum_bind

        if (
            not cb_id
            and self._ctx.in_forum
            and self._ctx.thread
            and text
            and not text.startswith("/")
            and not text.startswith("#REGISTER#")
            and not text.startswith("#WSS_LOOKUP#")
            and not text.startswith("do_rename:")
            and not text.startswith("forum_bind:")
        ):
            pending_node = self._pending_rename.get(self._ctx.thread)
            if pending_node:
                alias = sanitize_alias(text.replace("_", "-"), 20)
                if alias:
                    self._pending_rename.pop(self._ctx.thread, None)
                    self._delete_user_msg(user_msg_id)
                    user_msg_deleted = True
                    auth = self._auth_key(chat_id)
                    self._cmd_do_rename(chat_id, f"do_rename:{pending_node}:{alias}", auth)
                    return

        # 先应答 callback，消除客户端 loading；避免未传 text 导致 TypeError
        if cb_id:
            self.tg.answer_callback(cb_id)

        if self.handle_wss_lookup(chat_id, text):
            return

        if self.handle_register(chat_id, text):
            return

        auth = self._auth_key(chat_id)
        handled = False

        if text in ("/start", "/menu"):
            if self._ctx.in_forum and self._ctx.thread:
                node = self._node_for_thread(self._ctx.thread)
                if node:
                    self._cmd_manage(chat_id, node, None)
                    if user_msg_id:
                        self._delete_user_msg(user_msg_id)
                        user_msg_deleted = True
                    handled = True
            if not handled:
                self._cmd_start(chat_id, msg_id)
                handled = True
        elif text == "ignore":
            handled = True
        elif text == "all_ota_confirm":
            self._cmd_all_ota_confirm(chat_id, msg_id)
            handled = True
        elif text == "all_ota_execute":
            self._cmd_all_ota_execute(chat_id)
            handled = True
        elif text == "master_ota_confirm":
            self._cmd_master_ota_confirm(chat_id, msg_id)
            handled = True
        elif text == "master_ota_execute":
            self._cmd_master_ota_execute(chat_id, msg_id)
            handled = True
        elif text == "all_reports":
            self._cmd_all_reports(chat_id, msg_id)
            handled = True
        elif text == "all_run":
            self._cmd_all_run(chat_id)
            handled = True
        elif text.startswith("/quality"):
            self._cmd_quality(chat_id, text)
            handled = True
        elif text.startswith("/trend"):
            self._cmd_trend(chat_id, text)
            handled = True
        elif text == "list_nodes":
            self._cmd_list_nodes(chat_id, msg_id)
            handled = True
        elif text.startswith("region:"):
            self._cmd_region(chat_id, text.split(":", 1)[1], msg_id)
            handled = True
        elif text.startswith("manage:"):
            self._cmd_manage(chat_id, text.split(":", 1)[1], msg_id)
            handled = True
        elif text.startswith("toggle:"):
            self._cmd_toggle(chat_id, text, msg_id, auth)
            handled = True
        elif text.startswith("del:"):
            self._cmd_del(chat_id, text.split(":", 1)[1], msg_id)
            handled = True
        elif text.startswith("rename:"):
            self._cmd_rename(chat_id, text.split(":", 1)[1])
            handled = True
        elif text.startswith("do_rename:"):
            self._cmd_do_rename(chat_id, text, auth)
            handled = True
        elif text.startswith("ota_confirm:"):
            self._cmd_ota_confirm(chat_id, text.split(":", 1)[1])
            handled = True
        elif text.startswith("ota_execute:"):
            self._cmd_ota_execute(chat_id, text.split(":", 1)[1], msg_id, auth)
            handled = True
        elif text.startswith("trend:"):
            self._cmd_trend_callback(chat_id, text.split(":", 1)[1], msg_id)
            handled = True
        elif text.startswith("log_refresh:"):
            self._cmd_log_refresh(chat_id, text.split(":", 1)[1], msg_id, auth)
            handled = True
        elif text == "forum_topics_rebuild":
            self._cmd_forum_topics_rebuild(chat_id, msg_id, auth)
            handled = True
        elif text == "forum_bind_prompt":
            self._cmd_forum_bind_prompt(chat_id)
            handled = True
        elif text.startswith("forum_bind:"):
            self._cmd_forum_bind(chat_id, text.split(":", 1)[1], auth)
            handled = True
        elif any(text.startswith(p) for p in ("google:", "trust:", "run:", "report:", "log:", "quality:")):
            self._cmd_agent_action(chat_id, text, msg_id, auth)
            handled = True

        if handled and user_msg_id and self._ctx.in_forum and not user_msg_deleted and not cb_id:
            self._delete_user_msg(user_msg_id)

        if not handled and text:
            if self._is_node_topic():
                node = self._node_for_thread(self._ctx.thread)
                if node:
                    self._msg_node(
                        chat_id,
                        node,
                        "⚠️ 未识别的指令。发送 `/start@Bot` 打开控制台。",
                        markdown=False,
                    )
                    self._delete_user_msg(user_msg_id)
            elif self._ctx.chat == self.forum_chat_id:
                self._forum_menu(
                    "⚠️ 未识别的指令。发送 `/start@Bot` 打开主菜单。",
                    [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]],
                )
                self._delete_user_msg(user_msg_id)
            else:
                self._notify_owner("未识别的指令，请发送 /start 打开菜单。", markdown=False)

    def _cmd_start(self, chat_id: str, msg_id: int | None = None) -> None:
        remote = self._remote_version()
        ver = f"当前版本: `{self.version}`"
        if remote:
            if remote != self.version:
                ver += f"\n✨ **发现新版本**: `{remote}`"
            else:
                ver += "\n✅ 已是最新版本（仍可手动 OTA 以修复或重载）"

        count = self.db.scalar("SELECT COUNT(*) FROM nodes WHERE chat_id=?", (chat_id,)) or 0
        kb: list = []
        if not self.official and self.master_ota:
            ota_label = f"🆙 升级本机 Master → v{remote}" if remote and remote != self.version else "🆙 升级本机 Master"
            kb.append([{"text": ota_label, "callback_data": "master_ota_confirm"}])

        row2 = [
            {"text": "🚀 全部执行维护", "callback_data": "all_run"},
            {"text": "📊 全部生成报告", "callback_data": "all_reports"},
        ]
        if not self.official:
            row2.append({"text": "🔄 全部节点 OTA", "callback_data": "all_ota_confirm"})
        kb += [
            [{"text": "🌍 管理节点", "callback_data": "list_nodes"}],
            row2,
        ]
        if self.forum_mode:
            missing = self._count_nodes_missing_live_topic(chat_id)
            topic_label = (
                f"📌 补建节点话题 ({missing})" if missing else "📌 同步节点话题绑定"
            )
        else:
            topic_label = "📌 开启节点话题模式"
        kb.append([{"text": topic_label, "callback_data": "forum_topics_rebuild"}])
        kb.append(
            [{"text": "🌟 前往 GitHub 点亮星标", "url": "https://github.com/lasitan/IP-Sentinel"}],
        )
        msg = (
            f"🛡️ **IP-Sentinel Master**\n{ver}\n\n"
            f"📊 已注册节点: `{count}` 台\n请选择操作："
        )
        if self.forum_mode:
            msg += (
                f"\n\n📌 **话题模式已开启**\n"
                f"群组 `{self.forum_chat_id}` 中每个节点有独立话题；"
                "控制台、日志与报告均在对应节点话题内维护（General 仅作操作入口）。"
            )
        dest, thread = self._reply_chat()
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(msg, kb, msg_id)
            return
        if self.forum_mode and self.forum_chat_id:
            self._show_global_menu(msg, kb, msg_id)
            return
        if msg_id:
            self.tg.edit_ui(dest, msg_id, msg, kb, message_thread_id=thread)
        else:
            self.tg.send_ui(dest, msg, kb, message_thread_id=thread)

    def _remote_version(self) -> str:
        """拉取 GitHub 最新 MASTER_VERSION，带重试与缓存（60 秒内复用）."""
        import time as _time
        cache = getattr(self, "_remote_ver_cache", ("", 0.0))
        if cache[0] and _time.monotonic() - cache[1] < 60:
            return cache[0]
        url = f"{REPO_RAW_URL}/version.txt"
        headers = {"User-Agent": "IP-Sentinel-Master/1.0", "Cache-Control": "no-cache"}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    for line in resp.read().decode().splitlines():
                        if line.startswith("MASTER_VERSION="):
                            ver = line.split("=", 1)[1].strip().strip('"')
                            self._remote_ver_cache = (ver, _time.monotonic())
                            return ver
            except (OSError, urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    import time as _t
                    _t.sleep(1)
        return ""

    def _cmd_all_ota_confirm(self, chat_id: str, msg_id: int | None = None) -> None:
        kb = [
            [{"text": "🚨 确认执行", "callback_data": "all_ota_execute"}],
            [{"text": "取消操作", "callback_data": "/start"}],
        ]
        warn = (
            "**全部节点 OTA 升级**\n\n"
            "将向所有已开启 OTA 的节点下发升级指令。\n\n"
            "⚠️ **注意**：\n"
            "1. 升级期间 Agent 会短暂重启。\n"
            "2. 若无法访问 GitHub，部分节点需手动升级。\n\n"
            "**是否继续？**"
        )
        if msg_id:
            dest, thread = self._reply_chat()
            if self._ctx.chat == self.forum_chat_id:
                self._forum_menu(warn, kb, msg_id)
            else:
                self.tg.edit_ui(dest, msg_id, warn, kb, message_thread_id=thread)
        else:
            dest, thread = self._reply_chat()
            if self._ctx.chat == self.forum_chat_id:
                self._forum_menu(warn, kb)
            else:
                self.tg.send_ui(dest, warn, kb, message_thread_id=thread)

    def _cmd_all_ota_execute(self, chat_id: str) -> None:
        rows = self.db.execute(
            "SELECT 1 FROM nodes WHERE chat_id=? AND enable_ota='true' LIMIT 1", (chat_id,)
        )
        dest, thread = self._reply_chat()
        back = [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]]
        if not rows:
            msg = "⚠️ 您名下暂无开启 OTA 权限的在线节点。"
            if self._ctx.chat == self.forum_chat_id:
                self._forum_menu(msg, back)
            else:
                self.tg.send_message(dest, msg, message_thread_id=thread)
            return
        msg = (
            "📢 正在向全部节点下发 OTA 升级指令…\n"
            "*(完成后节点会发送新的注册消息)*"
        )
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(msg, back)
        else:
            self.tg.send_message(dest, msg, message_thread_id=thread)
        self._fanout_agents(chat_id, "/trigger_ota", filter_ota=True, delay=0.3)

    def _cmd_master_ota_confirm(self, chat_id: str, msg_id: int | None) -> None:
        if self.official:
            self.tg.send_message(
                chat_id,
                "⚠️ 官方公共网关未开放 Master 自升级，请使用私有 Master。",
                markdown=False,
            )
            return
        if not self.master_ota:
            self.tg.send_message(
                chat_id,
                "⚠️ 安装时未开启 Master OTA。请 SSH 执行 install_master.sh 重新安装并启用，或手动升级。",
                markdown=False,
            )
            return

        remote = self._remote_version()
        target = remote or self.version
        kb = [
            [{"text": "🚨 确认升级", "callback_data": "master_ota_execute"}],
            [{"text": "取消操作", "callback_data": "/start"}],
        ]
        same_ver = remote == self.version if remote else True
        extra = (
            "\n\n💡 云端版本与当前一致，将重新拉取并覆盖程序（可用于修复异常）。"
            if same_ver
            else ""
        )
        warn = (
            f"**Master OTA 升级**\n\n"
            f"当前: `{self.version}` → 目标: `{target}`\n\n"
            "将拉取最新安装脚本与 Python 代码并重启本机 Master。\n\n"
            "⚠️ 升级期间约 3–5 秒无法响应。"
            f"{extra}\n\n"
            "**是否继续？**"
        )
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, warn, kb)
        else:
            self.tg.send_ui(chat_id, warn, kb)

    def _cmd_master_ota_execute(self, chat_id: str, msg_id: int | None) -> None:
        if self.official or not self.master_ota:
            self.tg.send_message(chat_id, "⚠️ 当前环境不允许 Master OTA。", markdown=False)
            return

        note = "⏳ 正在下载安装脚本，Master 即将重启…"
        if msg_id:
            self.tg.edit_message(chat_id, msg_id, note)
        else:
            self.tg.send_message(chat_id, note)

        install_path = "/tmp/install_master.sh"
        try:
            subprocess.run(
                ["curl", "-fsSL", f"{REPO_RAW_URL}/master/install_master.sh", "-o", install_path],
                check=True,
                timeout=60,
            )
            chk = subprocess.run(["bash", "-n", install_path], capture_output=True, check=False)
            if chk.returncode != 0:
                err = "❌ 安装脚本校验失败，已取消升级。"
                if msg_id:
                    self.tg.edit_message(chat_id, msg_id, err)
                else:
                    self.tg.send_message(chat_id, err)
                return
            os.chmod(install_path, 0o755)
            env = f"export SILENT_MASTER_OTA='true'; export OTA_CHAT_ID='{chat_id}'; bash {install_path}"
            if shutil.which("systemd-run"):
                subprocess.Popen(["systemd-run", "--quiet", "--no-block", "bash", "-c", env])
            else:
                subprocess.Popen(["bash", "-c", env], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError):
            self.tg.send_message(chat_id, "❌ OTA 下载 install_master.sh 失败。")

    def _cmd_all_reports(self, chat_id: str, msg_id: int | None = None) -> None:
        back = [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]]
        if not self.db.scalar("SELECT 1 FROM nodes WHERE chat_id=? LIMIT 1", (chat_id,)):
            msg = "⚠️ 您名下暂无在线节点。"
            self._show_global_menu(msg, back, msg_id)
            return
        in_general = (
            self._ctx.chat == self.forum_chat_id and not self._is_node_topic()
        )
        if in_general:
            msg = (
                "📢 已向各节点话题下发报告请求。\n"
                "请进入对应节点话题查看（依次生成，请稍候）。"
            )
            self._forum_menu(msg, back, msg_id)
        else:
            msg = "📢 正在向全部节点并发下发报告请求，请稍候…"
            self._show_global_menu(msg, back, msg_id)
        self._fanout_reports(chat_id)

    def _cmd_all_run(self, chat_id: str) -> None:
        back = [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]]
        if not self.db.scalar("SELECT 1 FROM nodes WHERE chat_id=? LIMIT 1", (chat_id,)):
            msg = "⚠️ 您名下暂无在线节点。"
            self._show_global_menu(msg, back)
            return
        msg = "📢 正在向全部节点下发维护任务…"
        self._show_global_menu(msg, back)
        self._fanout_agents(chat_id, "/trigger_run", delay=0.2)

    def _node_ip(self, chat_id: str, node: str) -> str | None:
        return self.db.scalar(
            "SELECT agent_ip FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1",
            (chat_id, node),
        )

    def _format_agent_resp(self, resp: str, node: str, action: str) -> str:
        if resp == "FAILED":
            return f"❌ 节点 `{node}` WSS 未连接或指令超时，请确认 Agent 在线且已通过 TG 确认 Master 公网。"
        if "503" in resp or "missing" in resp.lower():
            scripts = {
                "google": "mod_google.py",
                "trust": "mod_trust.py",
                "quality": "mod_quality.py",
                "report": "report.py",
                "run": "runner.py",
            }
            name = scripts.get(action, action)
            return f"❌ 节点 `{node}` 缺少 `{name}`，请 OTA 升级。"
        if "403" in resp:
            return "⚠️ **拒绝执行**：该节点未在本地开启此模块，请检查安装时的配置！"
        ok_msgs = {
            "google": f"✅ 节点 `{node}` 回应: 📍 Google 纠偏程序启动。",
            "run": f"✅ 节点 `{node}` 回应: 📍 立即巡逻已触发。",
            "trust": f"✅ 节点 `{node}` 回应: 🛡️ IP 信用净化程序启动。",
            "quality": f"✅ 节点 `{node}` 已启动 IP 质量检测，结果将异步推送。",
            "report": f"✅ 节点 `{node}` 正在生成日报…",
            "log": f"✅ 节点 `{node}` 正在抓取日志...",
        }
        return ok_msgs.get(action, f"✅ 节点 `{node}` 接收指令: {action}")

    def _cmd_quality(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        node = sanitize_node_name(parts[1]) if len(parts) > 1 else ""
        if not node:
            self._notify_owner(
                "⚠️ 请指定节点，例如: `/quality HK-1`\n或在节点列表中选择。",
                markdown=False,
            )
            return
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, "❌ 数据库中未找到该节点的通讯地址。")
            return
        self._msg_node(
            chat_id, node, f"⏳ 正在向 `{node}` 下发 [quality] 指令，请稍候..."
        )
        resp = call_agent(chat_id, node, "/trigger_quality")
        self._msg_node(
            chat_id, node, self._format_agent_resp(resp, node, "quality")
        )

    def _cmd_trend(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        node = sanitize_node_name(parts[1]) if len(parts) > 1 else ""
        if not node:
            self._notify_owner(
                "⚠️ 请指定节点，例如: `/trend HK-1`\n或在节点列表中选择。",
                markdown=False,
            )
            return
        body = self._trend_text(chat_id, node)
        if body.startswith("⚠️"):
            dest, thread = self._menu_dest()
            self.tg.send_message(dest, body, message_thread_id=thread)
            return
        kb = [[{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node}"}]]
        dest, thread = self._menu_dest()
        self.tg.send_ui(dest, body, kb, message_thread_id=thread)

    def _cmd_trend_callback(self, chat_id: str, node: str, msg_id: int | None) -> None:
        node = sanitize_node_name(node)
        body = self._trend_text(chat_id, node)
        kb = [[{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node}"}]]
        if self._in_topic_flow(chat_id, node):
            self._ui_node(chat_id, node, body, kb)
            return
        dest, thread = self._menu_dest()
        if msg_id and dest == self._ctx.chat and self._ctx.thread == thread:
            self.tg.edit_ui(dest, msg_id, body, kb, message_thread_id=thread)
        else:
            self.tg.send_ui(dest, body, kb, message_thread_id=thread)

    def _cmd_list_nodes(self, chat_id: str, msg_id: int | None = None) -> None:
        kb = self._region_keyboard(chat_id, home_btn=True)
        body = "🌍 **按区域查看节点**\n请选择区域："
        if not kb:
            msg = "⚠️ 您名下暂无在线节点，请先在边缘机执行部署。"
            if self._ctx.chat == self.forum_chat_id:
                self._forum_menu(
                    msg,
                    [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]],
                    msg_id,
                )
            else:
                self._notify_owner(msg, markdown=False)
            return
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(body, kb, msg_id)
            return
        self._show_global_menu(body, kb, msg_id)

    def _cmd_region(self, chat_id: str, region: str, msg_id: int | None = None) -> None:
        region = sanitize_region(region)
        rows = self.db.execute(
            "SELECT node_name, COALESCE(node_alias, node_name) AS alias FROM nodes WHERE chat_id=? AND region=?",
            (chat_id, region),
        )
        if not rows:
            msg = "⚠️ 该区域下暂无节点。"
            if self._ctx.chat == self.forum_chat_id:
                self._forum_menu(
                    msg,
                    [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]],
                    msg_id,
                )
            else:
                self._notify_owner(msg)
            return
        kb: list = []
        row_btns: list = []
        for row in rows:
            row_btns.append(
                {"text": f"🖥️ {row['alias']}", "callback_data": f"manage:{row['node_name']}"}
            )
            if len(row_btns) == 2:
                kb.append(row_btns)
                row_btns = []
        if row_btns:
            kb.append(row_btns)
        kb.append(
            [
                {"text": "⬅️ 返回区域列表", "callback_data": "list_nodes"},
                {"text": "🏠 返回主菜单", "callback_data": "/start"},
            ]
        )
        body = f"📍 **[{region}] 节点列表**\n请选择节点："
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(body, kb, msg_id)
            return
        self._show_global_menu(body, kb, msg_id)

    def _cmd_manage(self, chat_id: str, node: str, msg_id: int | None) -> None:
        node = sanitize_node_name(node)
        use_topic = self._forum_node_active(chat_id, node)
        text, kb = self._manage_keyboard(chat_id, node, for_topic=use_topic)
        if not kb:
            self._notify_owner("❌ 未找到节点。")
            return
        if use_topic:
            self._pending_rename.pop(self._node_thread_id(chat_id, node) or 0, None)
            self._ui_node(chat_id, node, text, kb, on_manage=True)
            return
        if self.forum_mode and self.forum_chat_id:
            self._show_global_menu(text, kb, msg_id)
            return
        if msg_id and self._ctx.chat == self._ctx.owner:
            self.tg.edit_ui(chat_id, msg_id, text, kb)
        else:
            self.tg.send_ui(chat_id, text, kb)

    def _cmd_toggle(self, chat_id: str, text: str, msg_id: int | None, auth: str) -> None:
        parts = text.split(":")
        if len(parts) < 4:
            self._notify_owner("❌ 按钮数据无效，请返回节点面板重试。", markdown=False)
            return
        _, mod, node, state = parts[0], parts[1], parts[2], parts[3]
        node = sanitize_node_name(node)
        if mod not in ("google", "trust") or state not in ("true", "false"):
            self._notify_owner("❌ 无效的模块开关参数。", markdown=False)
            return
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, f"❌ 未找到节点 `{node}`。")
            return
        resp = call_agent(
            chat_id, node, "/trigger_toggle", {"mod": mod, "state": state}
        )
        if "Action Accepted" not in resp:
            self._msg_node(
                chat_id, node, "❌ 指令下发失败，请确认 Agent WSS 在线。", markdown=False
            )
            return
        col = "enable_google" if mod == "google" else "enable_trust"
        self.db.execute(
            f"UPDATE nodes SET {col}=? WHERE chat_id=? AND node_name=?",
            (state, chat_id, node),
        )
        base, kb = self._manage_keyboard(
            chat_id, node, for_topic=self._in_topic_flow(chat_id, node)
        )
        text_msg = base.replace(
            "请选择操作：",
            f"✅ **执行成功**: 模块 [{mod}] 已设为 {state}\n",
        )
        if self._forum_node_active(chat_id, node):
            self._ui_node(chat_id, node, text_msg, kb, on_manage=True)
            return
        if self.forum_mode and self.forum_chat_id:
            self._show_global_menu(text_msg, kb, msg_id)
            return
        if msg_id and self._ctx.chat == self._ctx.owner:
            self.tg.edit_ui(chat_id, msg_id, text_msg, kb)
        else:
            self.tg.send_ui(chat_id, text_msg, kb)

    def _cmd_del(self, chat_id: str, node: str, msg_id: int | None = None) -> None:
        node = sanitize_node_name(node)
        ok = self.db.scalar(
            "SELECT 1 FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1",
            (chat_id, node),
        )
        if not ok:
            self.tg.send_message(chat_id, "⛔ **安全拦截**：销毁失败。目标节点不存在或您无权越权操作！")
            return
        thread = self._node_thread_id(chat_id, node)
        ui_id, _ = self._get_topic_ui(chat_id, node)
        if thread and ui_id and self.forum_chat_id:
            self.tg.replace_message(
                self.forum_chat_id,
                ui_id,
                f"🗑️ 已删除节点 `{node}` 及其历史记录。",
                message_thread_id=thread,
            )
        self.db.execute("DELETE FROM nodes WHERE chat_id=? AND node_name=?", (chat_id, node))
        self.db.execute("DELETE FROM ip_trend_log WHERE node_name=?", (node,))
        if self._ctx.in_forum:
            return
        self.tg.send_message(chat_id, f"🗑️ 已删除节点 `{node}` 及其历史记录。")
        kb = self._region_keyboard(chat_id, home_btn=True)
        if kb:
            body = "🌍 节点列表："
            dest, thread = self._menu_dest()
            if msg_id and dest == self._ctx.chat:
                self.tg.edit_ui(dest, msg_id, body, kb, message_thread_id=thread)
            else:
                self.tg.send_ui(dest, body, kb, message_thread_id=thread)
        else:
            self.tg.send_message(chat_id, "⚠️ 当前没有任何已注册节点。", markdown=False)

    def _cmd_rename(self, chat_id: str, node: str) -> None:
        node = sanitize_node_name(node)
        thread = self._node_thread_id(chat_id, node)
        if self._in_topic_flow(chat_id, node) and thread:
            self._pending_rename[thread] = node
            self._ui_node(
                chat_id,
                node,
                f"✏️ **重命名节点** `{node}`\n\n请直接发送新别名（最长20字符）。",
                [[{"text": "❌ 取消", "callback_data": f"manage:{node}"}]],
            )
            return
        dest, thread = self._node_tg_dest(chat_id, node)
        self.tg.force_reply_rename(dest, node, message_thread_id=thread)

    def _cmd_do_rename(self, chat_id: str, text: str, auth: str) -> None:
        parts = text.split(":", 2)
        if len(parts) < 3:
            return
        node = sanitize_node_name(parts[1])
        alias = sanitize_alias(parts[2], 20)
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, "❌ 数据库中未找到该节点的通讯地址。")
            return
        if self._forum_node_active(chat_id, node):
            self._msg_node(chat_id, node, f"⏳ 正在向节点 `{node}` 下发重命名指令…")
        else:
            self._notify_owner(f"⏳ 正在向节点 `{node}` 下发重命名指令…")
        resp = call_agent(
            chat_id, node, "/trigger_rename", {"b64": alias_to_b64(alias)}
        )
        if resp == "FAILED":
            result = "❌ 指令下发超时！为防范劫持风险，已终止请求。"
        elif "Action Accepted" in resp:
            self.db.execute(
                "UPDATE nodes SET node_alias=? WHERE chat_id=? AND node_name=?",
                (alias, chat_id, node),
            )
            result = f"✅ 节点别名已更新为: `{alias}`"
        else:
            result = f"⚠️ 节点拒绝了请求，请确保 Agent 已更新至 v3.5.2\n(回传信息: `{resp[:200]}`)"
        if self._forum_node_active(chat_id, node):
            if "Action Accepted" in resp:
                self._cmd_manage(chat_id, node, None)
            else:
                self._msg_node(chat_id, node, result, markdown=not result.startswith("❌"))
        else:
            self._notify_owner(result, markdown=not result.startswith("❌"))

    def _cmd_ota_confirm(self, chat_id: str, node: str) -> None:
        node = sanitize_node_name(node)
        kb = [
            [{"text": "🚨 确认执行远程升级", "callback_data": f"ota_execute:{node}"}],
            [{"text": "❌ 取消", "callback_data": f"manage:{node}"}],
        ]
        body = (
            f"☢️ **操作确认**：即将向 `{node}` 下发 OTA 热更新指令。\n"
            "节点更新完成后会自动发送包含新版本号的注册回执，确定执行？"
        )
        if self._forum_node_active(chat_id, node):
            self._ui_node(chat_id, node, body, kb)
            return
        if self.forum_mode and self.forum_chat_id:
            self._show_global_menu(body, kb)
            return
        self.tg.send_ui(chat_id, body, kb)

    def _cmd_ota_execute(self, chat_id: str, node: str, msg_id: int | None, auth: str) -> None:
        node = sanitize_node_name(node)
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, "❌ 数据库中未找到该节点的通讯地址。")
            return
        wait = f"⏳ 正在向 `{node}` 发送 OTA 触发报文..."
        self._msg_node(chat_id, node, wait)
        resp = call_agent(chat_id, node, "/trigger_ota")
        if resp == "FAILED":
            result = "❌ OTA 指令下发失败：Agent WebSocket 未连接或超时。"
        elif "403" in resp:
            result = "⚠️ **节点拒绝执行**：该节点本地未开启 OTA 权限或运行在官方网关下！"
        else:
            result = "✅ OTA 已触发，节点正在后台升级…"
        self._msg_node(chat_id, node, result, markdown=False if "❌" in result else True)

    def handle_forum_bind_reply(self, text: str, reply_text: str) -> str | None:
        if "请回复本消息以绑定话题群组" not in reply_text:
            return None
        gid = sanitize_chat_id(text.strip())
        if not gid.startswith("-"):
            return None
        return f"forum_bind:{gid}"

    def _apply_forum_config(self, forum_chat_id: str, owner_chat_id: str) -> None:
        save_master_config_keys(
            {
                "FORUM_MODE": "true",
                "FORUM_CHAT_ID": forum_chat_id,
                "FORUM_OWNER_CHAT_ID": owner_chat_id,
            },
        )
        self.cfg["FORUM_MODE"] = "true"
        self.cfg["FORUM_CHAT_ID"] = forum_chat_id
        self.cfg["FORUM_OWNER_CHAT_ID"] = owner_chat_id

    def _cmd_forum_bind_prompt(self, chat_id: str) -> None:
        self.tg.force_reply_prompt(
            chat_id,
            "📝 **请回复本消息以绑定话题群组**\n"
            "粘贴超级群组的 Chat ID（通常以 `-100` 开头）。\n"
            "可通过 @RawDataBot 或 @getidsbot 获取。",
        )

    def _cmd_forum_bind(self, chat_id: str, raw_id: str, auth: str) -> None:
        forum_id = sanitize_chat_id(raw_id)
        if not forum_id.startswith("-"):
            self.tg.send_message(
                chat_id,
                "❌ Chat ID 格式无效，请以 `-100` 开头的群组 ID 重试。",
            )
            return
        self._apply_forum_config(forum_id, chat_id)
        self.tg.send_message(
            chat_id,
            f"✅ 话题模式已开启，绑定群组 `{forum_id}`。\n"
            "正在为现有节点补建话题…",
        )
        self._cmd_forum_topics_rebuild(chat_id, None, auth)

    def _cmd_forum_topics_rebuild(self, chat_id: str, msg_id: int | None, auth: str) -> None:
        """按 Telegram 当前话题列表匹配/补建节点话题，并同步 Agent 绑定."""
        if not self.forum_mode:
            kb = [
                [{"text": "📝 绑定群组 Chat ID", "callback_data": "forum_bind_prompt"}],
                [{"text": "🏠 返回主菜单", "callback_data": "/start"}],
            ]
            body = (
                "📌 **节点话题模式**\n\n"
                "每个节点在超级群组中拥有独立话题，日志/报告/控制台均在话题内维护。\n\n"
                "**前置条件：**\n"
                "1. 创建超级群组并开启 **Topics**\n"
                "2. 将 Bot 设为管理员（含「管理话题」权限）\n"
                "3. 获取群组 Chat ID 并点击下方按钮绑定"
            )
            if msg_id:
                self.tg.edit_ui(chat_id, msg_id, body, kb)
            else:
                self.tg.send_ui(chat_id, body, kb)
            return
        rows = self.db.execute(
            """SELECT node_name, COALESCE(node_alias, node_name) AS alias, region,
                      message_thread_id
               FROM nodes WHERE chat_id=? ORDER BY node_name""",
            (chat_id,),
        )
        if not rows:
            self.tg.send_message(chat_id, "⚠️ 暂无已注册节点。")
            return

        wait = f"⏳ 正在从 Telegram 拉取话题列表并处理 {len(rows)} 个节点…"
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(
                wait,
                [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]],
                msg_id,
            )
        elif msg_id:
            self.tg.edit_message(chat_id, msg_id, wait)
        else:
            self.tg.send_message(chat_id, wait)

        live_topics = self._fetch_forum_topics_map()
        if not live_topics and rows:
            print(
                "[ip-sentinel-master] getForumTopics 未返回话题，将尝试为缺失节点新建",
                flush=True,
            )

        matched = 0
        created = 0
        failed: list[str] = []
        for row in rows:
            node = row["node_name"]
            alias = row["alias"]
            region = row["region"] or "UNKNOWN"
            db_thread = int(row["message_thread_id"]) if row["message_thread_id"] else None
            existed_in_tg = bool(
                self._resolve_node_thread_from_topics(alias, region, db_thread, live_topics)
            )

            thread_id = self._setup_node_topic(
                chat_id,
                node,
                auth,
                alias=alias,
                region=region,
                send_console=not existed_in_tg,
                live_topics=live_topics,
            )
            if thread_id:
                if existed_in_tg:
                    matched += 1
                else:
                    created += 1
                    live_topics[self._topic_title(alias, region)] = thread_id
            else:
                failed.append(alias)
            time.sleep(0.35)

        lines = [
            "✅ **话题同步完成**",
            f"• 匹配 Telegram 已有话题: `{matched}`",
            f"• 新建话题: `{created}`",
            f"• 已同步 Agent 绑定: `{matched + created}`",
        ]
        if failed:
            lines.append(f"• 失败: `{', '.join(failed)}`")
            lines.append(
                "\n_失败时请确认 Bot 为群组管理员且已开启 Topics，然后重试。_"
            )
        result = "\n".join(lines)
        if self._ctx.chat == self.forum_chat_id:
            self._forum_menu(
                result,
                [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]],
                msg_id,
            )
        elif msg_id:
            if not self.tg.edit_message(chat_id, msg_id, result):
                self.tg.send_message(chat_id, result)
        else:
            self.tg.send_message(chat_id, result)

    def _cmd_log_refresh(self, chat_id: str, node: str, msg_id: int | None, auth: str) -> None:
        """刷新日志：Agent 直接删旧发新，无需 Master 预占位。"""
        node = sanitize_node_name(node)
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, "❌ 数据库中未找到该节点的通讯地址。")
            return
        resp = call_agent(chat_id, node, "/trigger_log", {"msg_id": str(msg_id or "")})
        if resp == "FAILED":
            self._msg_node(chat_id, node, "❌ 日志刷新失败：节点无响应或链路异常。", markdown=False)
        elif "503" in resp or "missing" in resp.lower():
            self._msg_node(
                chat_id,
                node,
                f"❌ 节点 `{node}` 缺少对应模块，请 OTA 升级。",
            )

    def _cmd_agent_action(self, chat_id: str, text: str, msg_id: int | None, auth: str) -> None:
        action = text.split(":", 1)[0]
        node = sanitize_node_name(text.split(":", 1)[1])
        if not self._node_ip(chat_id, node):
            self._msg_node(chat_id, node, "❌ 数据库中未找到该节点的通讯地址。")
            return
        agent_ip = self._node_ip(chat_id, node)
        # log / report / quality：Agent 异步推送结果到话题，Master 不占位，避免 "⏳" 永久卡住
        _agent_async = action in ("log", "report", "quality")
        _in_topic = self.forum_mode and bool(self._node_thread_id(chat_id, node))
        if not _agent_async or not _in_topic:
            wait = f"⏳ 正在向 `{node}` ({agent_ip}) 下发 [{action}] 指令，请稍候..."
            if action != "log":
                self._msg_node(chat_id, node, wait)
        resp = call_agent(chat_id, node, f"/trigger_{action}")
        result = self._format_agent_resp(resp, node, action)
        # 错误直接回显；成功且 Agent 异步处理的动作不再二次 msg_node
        if result.startswith("❌") or result.startswith("⚠️"):
            self._msg_node(chat_id, node, result, markdown=False)
        elif not _agent_async:
            self._msg_node(chat_id, node, result)
