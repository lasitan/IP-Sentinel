#!/usr/bin/env python3
"""Master Telegram 指令路由与业务处理."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from master.agent_client import call_agent
from master.db import MasterDB
from master.flags import get_flag
from master.security import (
    alias_to_b64,
    generate_signed_url,
    is_ssrf_ip,
    sanitize_agent_ip,
    sanitize_alias,
    sanitize_chat_id,
    sanitize_node_name,
    sanitize_port,
    sanitize_region,
    sanitize_score,
    sanitize_status_field,
)
from master.telegram_api import TelegramAPI

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
        return chat_id

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

    def _msg_node(
        self,
        owner_chat_id: str,
        node: str,
        text: str,
        *,
        markdown: bool = True,
    ) -> None:
        dest, thread = self._node_tg_dest(owner_chat_id, node)
        if self._can_edit_in_place(dest, thread):
            if not self.tg.edit_message(
                dest, self._ctx.msg_id, text, message_thread_id=thread
            ):
                self.tg.send_message(dest, text, markdown=markdown, message_thread_id=thread)
        else:
            self.tg.send_message(dest, text, markdown=markdown, message_thread_id=thread)
            if dest != owner_chat_id and not self._ctx.in_forum:
                alias = self.db.scalar(
                    "SELECT COALESCE(node_alias, node_name) FROM nodes WHERE chat_id=? AND node_name=?",
                    (owner_chat_id, node),
                ) or node
                self.tg.send_message(
                    owner_chat_id,
                    f"💬 节点 `{alias}` 的操作反馈已发送至群组话题。",
                )

    def _ui_node(
        self,
        owner_chat_id: str,
        node: str,
        text: str,
        keyboard: list,
    ) -> None:
        dest, thread = self._node_tg_dest(owner_chat_id, node)
        if self._can_edit_in_place(dest, thread):
            self.tg.edit_ui(dest, self._ctx.msg_id, text, keyboard, message_thread_id=thread)
            return
        self.tg.send_ui(dest, text, keyboard, message_thread_id=thread)
        if dest != owner_chat_id and not self._ctx.in_forum:
            alias = self.db.scalar(
                "SELECT COALESCE(node_alias, node_name) FROM nodes WHERE chat_id=? AND node_name=?",
                (owner_chat_id, node),
            ) or node
            self.tg.send_message(
                owner_chat_id,
                f"⚙️ 节点 `{alias}` 控制台已在群组话题中打开，请前往对应话题操作。",
            )

    def _push_topic_to_agent(self, owner_chat_id: str, node: str, auth: str) -> None:
        info = self._agent_row(owner_chat_id, node)
        thread = self._node_thread_id(owner_chat_id, node)
        if not info or not thread:
            return
        ip, port = info
        q = urllib.parse.urlencode(
            {"dest_chat": self.forum_chat_id, "thread_id": str(thread)}
        )
        url = generate_signed_url(auth, ip, port, "/trigger_set_topic") + "&" + q
        threading.Thread(target=call_agent, args=(url,), daemon=True).start()

    def _setup_node_topic(
        self,
        owner_chat_id: str,
        node: str,
        auth: str,
        *,
        alias: str | None = None,
        region: str | None = None,
        send_console: bool = True,
    ) -> int | None:
        """创建节点话题（若缺失）、同步 Agent 绑定，可选推送控制台到群组."""
        if not self.forum_mode or not self.forum_chat_id:
            return None
        thread_id = self._node_thread_id(owner_chat_id, node)
        if not thread_id:
            if alias is None or region is None:
                row = self.db.execute(
                    """SELECT COALESCE(node_alias, node_name) AS alias, region
                       FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1""",
                    (owner_chat_id, node),
                )
                if not row:
                    return None
                alias = row[0]["alias"]
                region = row[0]["region"] or "UNKNOWN"
            thread_id = self.tg.create_forum_topic(
                self.forum_chat_id, f"{alias} · {region}"
            )
            if not thread_id:
                return None
            self.db.execute(
                "UPDATE nodes SET message_thread_id=? WHERE chat_id=? AND node_name=?",
                (thread_id, owner_chat_id, node),
            )
        self._push_topic_to_agent(owner_chat_id, node, auth)
        if send_console:
            panel, kb = self._manage_keyboard(owner_chat_id, node)
            if kb:
                self.tg.send_ui(
                    self.forum_chat_id, panel, kb, message_thread_id=thread_id
                )
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

    def _manage_keyboard(self, chat_id: str, node: str) -> tuple[str, list]:
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
        danger = [
            [
                {"text": "🗑️ 删除节点", "callback_data": f"del:{node}"},
                {"text": "⬅️ 返回区域列表", "callback_data": "list_nodes"},
            ],
        ]
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
            "时间(本地)  | 风险 | 谷歌 | NF | GPT",
            "-----------------------------------------",
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
        delay: float = 0.2,
    ) -> None:
        sql = "SELECT node_name, agent_ip, agent_port FROM nodes WHERE chat_id=?"
        params: tuple = (chat_id,)
        if filter_ota:
            sql += " AND enable_ota='true'"
        rows = self.db.execute(sql, params)
        auth = self._auth_key(chat_id)

        def _worker() -> None:
            for row in rows:
                url = generate_signed_url(auth, row["agent_ip"], row["agent_port"], path)
                call_agent(url)
                time.sleep(delay)

        threading.Thread(target=_worker, daemon=True).start()

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
            _, region, node, ip, port, alias, ota = fields[:7]
        elif n == 6:
            _, region, node, ip, port, alias = fields[:6]
            ota = "false"
        elif n == 5:
            _, region, node, ip, port = fields[:5]
            alias, ota = node, "false"
        else:
            _, node, ip, port = fields[:4]
            region, alias, ota = "UNKNOWN", node, "false"

        region = sanitize_region(region)
        node = sanitize_node_name(node)
        ip = sanitize_agent_ip(ip)
        port = sanitize_port(port)
        alias = sanitize_alias(alias) or node
        ota = re.sub(r"[^a-z]", "", (ota or "false").lower()) or "false"

        if is_ssrf_ip(ip):
            self.tg.send_message(chat_id, "⛔ **安全拦截**：禁止注册内网或回环 IP，防止 SSRF 攻击渗透。")
            return True
        if not node or not ip or not port:
            self.tg.send_message(chat_id, "⛔ **安全拦截**：检测到非法注册载荷，请求已拒绝。")
            return True

        self.db.execute(
            """INSERT INTO nodes (chat_id, node_name, agent_ip, agent_port, last_seen,
                                  region, node_alias, enable_ota)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
               ON CONFLICT(chat_id, node_name) DO UPDATE SET
                 agent_ip=excluded.agent_ip, agent_port=excluded.agent_port,
                 last_seen=CURRENT_TIMESTAMP, region=excluded.region,
                 node_alias=excluded.node_alias, enable_ota=excluded.enable_ota""",
            (chat_id, node, ip, port, region, alias, ota),
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
            self.tg.send_ui(chat_id, "🌍 **按区域查看节点**\n请选择区域：", kb)
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
        reply_to_text: str = "",
    ) -> None:
        incoming_chat = sanitize_chat_id(chat_id)
        if not incoming_chat:
            return
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

        if text.startswith("svq|"):
            if self.handle_svq(chat_id, text, cb_id, msg_id):
                return

        rewritten = self.handle_rename_reply(text, reply_to_text)
        if rewritten:
            text = rewritten

        # 先应答 callback，消除客户端 loading；避免未传 text 导致 TypeError
        if cb_id:
            self.tg.answer_callback(cb_id)

        if self.handle_register(chat_id, text):
            return

        auth = self._auth_key(chat_id)
        handled = False

        if text in ("/start", "/menu"):
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
            self._cmd_all_reports(chat_id)
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
        elif any(text.startswith(p) for p in ("google:", "trust:", "run:", "report:", "log:", "quality:")):
            self._cmd_agent_action(chat_id, text, msg_id, auth)
            handled = True

        if not handled and text:
            self.tg.send_message(chat_id, "未识别的指令，请发送 /start 打开菜单。", markdown=False)

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
            missing = self.db.scalar(
                """SELECT COUNT(*) FROM nodes WHERE chat_id=?
                   AND (message_thread_id IS NULL OR message_thread_id = 0)""",
                (chat_id,),
            ) or 0
            label = f"📌 补建节点话题 ({missing})" if missing else "📌 同步节点话题绑定"
            kb.append([{"text": label, "callback_data": "forum_topics_rebuild"}])
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
                "节点控制台、日志与报告均在对应话题内维护。"
            )
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, msg, kb)
        else:
            self.tg.send_ui(chat_id, msg, kb)

    def _remote_version(self) -> str:
        try:
            req = urllib.request.Request(f"{REPO_RAW_URL}/version.txt", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                for line in resp.read().decode().splitlines():
                    if line.startswith("MASTER_VERSION="):
                        return line.split("=", 1)[1].strip().strip('"')
        except (OSError, urllib.error.URLError):
            pass
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
            self.tg.edit_ui(chat_id, msg_id, warn, kb)
        else:
            self.tg.send_ui(chat_id, warn, kb)

    def _cmd_all_ota_execute(self, chat_id: str) -> None:
        rows = self.db.execute(
            "SELECT 1 FROM nodes WHERE chat_id=? AND enable_ota='true' LIMIT 1", (chat_id,)
        )
        if not rows:
            self.tg.send_message(chat_id, "⚠️ 您名下暂无开启 OTA 权限的在线节点。")
            return
        self.tg.send_message(
            chat_id,
            "📢 正在向全部节点下发 OTA 升级指令…\n"
            "*(完成后节点会发送新的注册消息)*",
        )
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

    def _cmd_all_reports(self, chat_id: str) -> None:
        if not self.db.scalar("SELECT 1 FROM nodes WHERE chat_id=? LIMIT 1", (chat_id,)):
            self.tg.send_message(chat_id, "⚠️ 您名下暂无在线节点。")
            return
        self.tg.send_message(
            chat_id,
            "📢 正在向全部节点请求报告…\n"
            "*(为避免 Telegram 限流，将依次发送，请稍候)*",
        )
        self._fanout_agents(chat_id, "/trigger_report", delay=2.0)

    def _cmd_all_run(self, chat_id: str) -> None:
        if not self.db.scalar("SELECT 1 FROM nodes WHERE chat_id=? LIMIT 1", (chat_id,)):
            self.tg.send_message(chat_id, "⚠️ 您名下暂无在线节点。")
            return
        self.tg.send_message(chat_id, "📢 正在向全部节点下发维护任务…")
        self._fanout_agents(chat_id, "/trigger_run", delay=0.2)

    def _agent_row(self, chat_id: str, node: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT agent_ip, agent_port FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1",
            (chat_id, node),
        )
        if not row:
            return None
        return row[0]["agent_ip"], row[0]["agent_port"]

    def _format_agent_resp(self, resp: str, node: str, action: str) -> str:
        if resp == "FAILED":
            return "❌ 指令下发超时或失败！为保护链路安全，已终止通信 (严禁降级为 HTTP)。"
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
            self.tg.send_message(
                chat_id,
                "⚠️ 请指定节点，例如: `/quality HK-1`\n或在节点列表中选择。",
            )
            return
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, "❌ 数据库中未找到该节点的通讯地址。")
            return
        ip, port = info
        self.tg.send_message(chat_id, f"⏳ 正在向 `{node}` ({ip}) 下发 [quality] 指令，请稍候...")
        url = generate_signed_url(self._auth_key(chat_id), ip, port, "/trigger_quality")
        resp = call_agent(url)
        self.tg.send_message(chat_id, self._format_agent_resp(resp, node, "quality"))

    def _cmd_trend(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        node = sanitize_node_name(parts[1]) if len(parts) > 1 else ""
        if not node:
            self.tg.send_message(
                chat_id,
                "⚠️ 请指定节点，例如: `/trend HK-1`\n或在节点列表中选择。",
            )
            return
        body = self._trend_text(chat_id, node)
        if body.startswith("⚠️"):
            self.tg.send_message(chat_id, body)
            return
        kb = [[{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node}"}]]
        self.tg.send_ui(chat_id, body, kb)

    def _cmd_trend_callback(self, chat_id: str, node: str, msg_id: int | None) -> None:
        node = sanitize_node_name(node)
        body = self._trend_text(chat_id, node)
        kb = [[{"text": "⚙️ 调出该节点控制台", "callback_data": f"manage:{node}"}]]
        if self.forum_mode and self._node_thread_id(chat_id, node):
            self._ui_node(chat_id, node, body, kb)
            return
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, body, kb)
        else:
            self.tg.send_ui(chat_id, body, kb)

    def _cmd_list_nodes(self, chat_id: str, msg_id: int | None = None) -> None:
        kb = self._region_keyboard(chat_id, home_btn=True)
        if not kb:
            self.tg.send_message(chat_id, "⚠️ 您名下暂无在线节点，请先在边缘机执行部署。", markdown=False)
            return
        body = "🌍 **按区域查看节点**\n请选择区域："
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, body, kb)
        else:
            self.tg.send_ui(chat_id, body, kb)

    def _cmd_region(self, chat_id: str, region: str, msg_id: int | None = None) -> None:
        region = sanitize_region(region)
        rows = self.db.execute(
            "SELECT node_name, COALESCE(node_alias, node_name) AS alias FROM nodes WHERE chat_id=? AND region=?",
            (chat_id, region),
        )
        if not rows:
            self.tg.send_message(chat_id, "⚠️ 该区域下暂无节点。")
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
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, body, kb)
        else:
            self.tg.send_ui(chat_id, body, kb)

    def _cmd_manage(self, chat_id: str, node: str, msg_id: int | None) -> None:
        node = sanitize_node_name(node)
        text, kb = self._manage_keyboard(chat_id, node)
        if not kb:
            self.tg.send_message(chat_id, "❌ 未找到节点。")
            return
        if self.forum_mode and self._node_thread_id(chat_id, node):
            self._ui_node(chat_id, node, text, kb)
            return
        if msg_id:
            self.tg.edit_ui(chat_id, msg_id, text, kb)
        else:
            self.tg.send_ui(chat_id, text, kb)

    def _cmd_toggle(self, chat_id: str, text: str, msg_id: int | None, auth: str) -> None:
        parts = text.split(":")
        if len(parts) < 4:
            self.tg.send_message(chat_id, "❌ 按钮数据无效，请返回节点面板重试。", markdown=False)
            return
        _, mod, node, state = parts[0], parts[1], parts[2], parts[3]
        node = sanitize_node_name(node)
        if mod not in ("google", "trust") or state not in ("true", "false"):
            self.tg.send_message(chat_id, "❌ 无效的模块开关参数。", markdown=False)
            return
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, f"❌ 未找到节点 `{node}`。", markdown=False)
            return
        ip, port = info
        url = generate_signed_url(auth, ip, port, "/trigger_toggle") + f"&mod={mod}&state={state}"
        resp = call_agent(url)
        if "Action Accepted" not in resp:
            self.tg.send_message(chat_id, "❌ 指令下发失败，请检查节点在线与防火墙。", markdown=False)
            return
        col = "enable_google" if mod == "google" else "enable_trust"
        self.db.execute(
            f"UPDATE nodes SET {col}=? WHERE chat_id=? AND node_name=?",
            (state, chat_id, node),
        )
        base, kb = self._manage_keyboard(chat_id, node)
        text_msg = base.replace(
            "请选择操作：",
            f"✅ **执行成功**: 模块 [{mod}] 已设为 {state}\n",
        )
        if self.forum_mode and self._node_thread_id(chat_id, node):
            self._ui_node(chat_id, node, text_msg, kb)
            return
        if msg_id:
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
        self.db.execute("DELETE FROM nodes WHERE chat_id=? AND node_name=?", (chat_id, node))
        self.db.execute("DELETE FROM ip_trend_log WHERE node_name=?", (node,))
        self.tg.send_message(chat_id, f"🗑️ 已删除节点 `{node}` 及其历史记录。")
        kb = self._region_keyboard(chat_id, home_btn=True)
        if kb:
            body = "🌍 节点列表："
            if msg_id:
                self.tg.edit_ui(chat_id, msg_id, body, kb)
            else:
                self.tg.send_ui(chat_id, body, kb)
        else:
            self.tg.send_message(chat_id, "⚠️ 当前没有任何已注册节点。", markdown=False)

    def _cmd_rename(self, chat_id: str, node: str) -> None:
        node = sanitize_node_name(node)
        dest, thread = self._node_tg_dest(chat_id, node)
        self.tg.force_reply_rename(dest, node, message_thread_id=thread)

    def _cmd_do_rename(self, chat_id: str, text: str, auth: str) -> None:
        parts = text.split(":", 2)
        if len(parts) < 3:
            return
        node = sanitize_node_name(parts[1])
        alias = sanitize_alias(parts[2], 20)
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, "❌ 数据库中未找到该节点的通讯地址。")
            return
        ip, port = info
        self.tg.send_message(chat_id, f"⏳ 正在向节点 `{node}` 下发重命名指令…")
        url = generate_signed_url(auth, ip, port, "/trigger_rename") + f"&b64={alias_to_b64(alias)}"
        resp = call_agent(url)
        if resp == "FAILED":
            self.tg.send_message(chat_id, "❌ 指令下发超时！为防范劫持风险，已终止请求。")
        elif "Action Accepted" in resp:
            self.db.execute(
                "UPDATE nodes SET node_alias=? WHERE chat_id=? AND node_name=?",
                (alias, chat_id, node),
            )
            self.tg.send_message(
                chat_id,
                f"✅ 节点别名已更新为: `{alias}`",
            )
        else:
            self.tg.send_message(
                chat_id,
                f"⚠️ 节点拒绝了请求，请确保 Agent 已更新至 v3.5.2\n(回传信息: `{resp[:200]}`)",
            )

    def _cmd_ota_confirm(self, chat_id: str, node: str) -> None:
        node = sanitize_node_name(node)
        kb = [
            [{"text": "🚨 确认执行远程升级", "callback_data": f"ota_execute:{node}"}],
            [{"text": "取消", "callback_data": f"manage:{node}"}],
        ]
        body = (
            f"☢️ **操作确认**：即将向 `{node}` 下发 OTA 热更新指令。\n"
            "节点更新完成后会自动发送包含新版本号的注册回执，确定执行？"
        )
        if self.forum_mode and self._node_thread_id(chat_id, node):
            self._ui_node(chat_id, node, body, kb)
            return
        self.tg.send_ui(chat_id, body, kb)

    def _cmd_ota_execute(self, chat_id: str, node: str, msg_id: int | None, auth: str) -> None:
        node = sanitize_node_name(node)
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, "❌ 数据库中未找到该节点的通讯地址。")
            return
        ip, port = info
        wait = f"⏳ 正在向 `{node}` 发送 OTA 触发报文..."
        self._msg_node(chat_id, node, wait)
        url = generate_signed_url(auth, ip, port, "/trigger_ota")
        resp = call_agent(url)
        if resp == "FAILED":
            result = "❌ OTA 指令下发彻底失败！链路异常或严禁使用 HTTP 降级通讯。"
        elif "403" in resp:
            result = "⚠️ **节点拒绝执行**：该节点本地未开启 OTA 权限或运行在官方网关下！"
        else:
            result = "✅ OTA 已触发，节点正在后台升级…"
        self._msg_node(chat_id, node, result, markdown=False if "❌" in result else True)

    def _cmd_forum_topics_rebuild(self, chat_id: str, msg_id: int | None, auth: str) -> None:
        """为缺少话题的节点批量补建 Forum Topic，并同步已有节点的 Agent 绑定."""
        if not self.forum_mode:
            self.tg.send_message(chat_id, "⚠️ 未开启话题模式。", markdown=False)
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

        wait = f"⏳ 正在处理 {len(rows)} 个节点的话题…"
        if msg_id:
            self.tg.edit_message(chat_id, msg_id, wait)
        else:
            self.tg.send_message(chat_id, wait)

        created = 0
        synced = 0
        failed: list[str] = []
        for row in rows:
            node = row["node_name"]
            had_topic = bool(row["message_thread_id"])
            thread_id = self._setup_node_topic(
                chat_id,
                node,
                auth,
                alias=row["alias"],
                region=row["region"] or "UNKNOWN",
                send_console=not had_topic,
            )
            if thread_id:
                if had_topic:
                    synced += 1
                else:
                    created += 1
            else:
                failed.append(row["alias"])
            time.sleep(0.35)

        lines = [
            "✅ **话题补建完成**",
            f"• 新建话题: `{created}`",
            f"• 已有话题（已同步 Agent）: `{synced}`",
        ]
        if failed:
            lines.append(f"• 失败: `{', '.join(failed)}`")
            lines.append(
                "\n_失败时请确认 Bot 为群组管理员且已开启 Topics，然后重试。_"
            )
        result = "\n".join(lines)
        if msg_id:
            if not self.tg.edit_message(chat_id, msg_id, result):
                self.tg.send_message(chat_id, result)
        else:
            self.tg.send_message(chat_id, result)

    def _cmd_log_refresh(self, chat_id: str, node: str, msg_id: int | None, auth: str) -> None:
        """在原日志消息上刷新内容，不新发互动消息."""
        if not msg_id:
            self.tg.send_message(chat_id, "⚠️ 无法刷新：缺少消息上下文。", markdown=False)
            return
        node = sanitize_node_name(node)
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, "❌ 数据库中未找到该节点的通讯地址。")
            return
        ip, port = info
        url = generate_signed_url(auth, ip, port, "/trigger_log") + f"&msg_id={msg_id}"
        resp = call_agent(url)
        if resp == "FAILED":
            self.tg.send_message(
                chat_id,
                "❌ 日志刷新失败：节点无响应或链路异常。",
                markdown=False,
            )
        elif "503" in resp or "missing" in resp.lower():
            self.tg.send_message(
                chat_id,
                f"❌ 节点 `{node}` 缺少 webhook 模块，请 OTA 升级。",
            )

    def _cmd_agent_action(self, chat_id: str, text: str, msg_id: int | None, auth: str) -> None:
        action = text.split(":", 1)[0]
        node = sanitize_node_name(text.split(":", 1)[1])
        info = self._agent_row(chat_id, node)
        if not info:
            self.tg.send_message(chat_id, "❌ 数据库中未找到该节点的通讯地址。")
            return
        ip, port = info
        wait = f"⏳ 正在向 `{node}` ({info[0]}) 下发 [{action}] 指令，请稍候..."
        if action == "log":
            if self.forum_mode and self._node_thread_id(chat_id, node):
                self._msg_node(chat_id, node, wait)
        else:
            self._msg_node(chat_id, node, wait)
        url = generate_signed_url(auth, ip, port, f"/trigger_{action}")
        resp = call_agent(url)
        result = self._format_agent_resp(resp, node, action)
        if action != "log":
            self._msg_node(chat_id, node, result, markdown=False if result.startswith("❌") else True)
