"""全节点日报汇总：静默采集各节点报告 + 推送精简总结."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from master.agent_client import agent_online, call_agent
from master.config import load_master_config
from master.db import MasterDB
from master.security import sanitize_chat_id
from master.telegram_api import TelegramAPI

# 全量日报生成含实时探针，单节点可能较慢
_SUMMARY_TIMEOUT = 120


def _forum_enabled(cfg: dict[str, Any]) -> bool:
    return (
        cfg.get("FORUM_MODE", "false").lower() == "true"
        and bool(sanitize_chat_id(str(cfg.get("FORUM_CHAT_ID", ""))))
    )


def _collect_one(owner: str, node: str) -> dict[str, Any]:
    """向 Agent 请求静默日报快照（不推送节点话题）."""
    if not agent_online(owner, node):
        return {"node": node, "error": "offline"}
    resp = call_agent(owner, node, "/trigger_report_summary", timeout=_SUMMARY_TIMEOUT)
    if resp == "FAILED":
        return {"node": node, "error": "timeout"}
    text = (resp or "").strip()
    if not text:
        return {"node": node, "error": "empty"}
    try:
        data = json.loads(text.splitlines()[0])
        if isinstance(data, dict):
            data.setdefault("node", node)
            return data
    except json.JSONDecodeError:
        pass
    return {"node": node, "error": "parse"}


def _unlock_bucket(item: dict[str, Any]) -> str:
    """在线节点解锁归类：songzhong | warn | ok."""
    unlock = item.get("unlock") or {}
    yt = str(unlock.get("yt") or "N/A")
    play = str(unlock.get("play") or "N/A")
    gemini = str(unlock.get("gemini") or "N/A")

    if unlock.get("cn") or yt == "送中":
        return "songzhong"
    if yt == "解锁" and play == "解锁" and gemini == "解锁":
        return "ok"
    return "warn"


def _count_unlock_stats(summaries: list[dict[str, Any]]) -> dict[str, int]:
    online_items = [s for s in summaries if not s.get("error")]
    counts = {"songzhong": 0, "warn": 0, "ok": 0}
    for item in online_items:
        bucket = _unlock_bucket(item)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _format_fleet_summary(
    total: int,
    online: int,
    unlock: dict[str, int],
    now: str,
) -> str:
    offline = total - online
    return (
        f"📊 **IP-Sentinel 全节点每日总结**\n"
        f"📅 `{now}`\n\n"
        f"📡 **在线统计**\n"
        f"共 **{total}** 节点 | 在线 **{online}** | 离线 **{offline}**\n\n"
        f"🔓 **解锁概况**（在线 {online} 节点）\n"
        f"🔴 **{unlock['songzhong']}** 个被判定为送中 | "
        f"⚠️ **{unlock['warn']}** 个解锁警告 | "
        f"✅ **{unlock['ok']}** 个解锁正常"
    )


def run_fleet_daily_report(cfg: dict[str, Any], db: MasterDB, tg: TelegramAPI) -> None:
    """全节点静默生成日报并汇总，仅推送总结至 General（或 owner 私聊）."""
    owners = db.execute("SELECT DISTINCT chat_id FROM nodes ORDER BY chat_id")
    if not owners:
        print("[ip-sentinel-master] 全节点总结：无注册节点，跳过", flush=True)
        return

    forum = _forum_enabled(cfg)
    forum_chat = sanitize_chat_id(str(cfg.get("FORUM_CHAT_ID", "")))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    for owner_row in owners:
        owner = str(owner_row["chat_id"])
        node_rows = db.execute(
            "SELECT node_name FROM nodes WHERE chat_id=? ORDER BY region, node_name",
            (owner,),
        )
        if not node_rows:
            continue
        nodes = [str(r["node_name"]) for r in node_rows]

        print(
            f"[ip-sentinel-master] 全节点总结：静默生成 {len(nodes)} 个节点报告…",
            flush=True,
        )

        summaries: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
            futs = {pool.submit(_collect_one, owner, n): n for n in nodes}
            for fut in as_completed(futs):
                try:
                    summaries.append(fut.result())
                except Exception as exc:
                    summaries.append({"node": futs[fut], "error": str(exc)[:40]})

        online = sum(1 for n in nodes if agent_online(owner, n))
        unlock = _count_unlock_stats(summaries)
        collected = sum(1 for s in summaries if not s.get("error"))
        unlock["warn"] += max(0, online - collected)
        msg = _format_fleet_summary(len(nodes), online, unlock, now)

        if forum and forum_chat:
            dest = forum_chat
            thread: int | None = None
        else:
            dest = owner
            thread = None

        kb = [[{"text": "🏠 返回主菜单", "callback_data": "/start"}]]
        tg.send_ui(dest, msg, kb, message_thread_id=thread)

        print(
            f"[ip-sentinel-master] 全节点总结已推送 owner={owner} "
            f"(在线 {online}/{len(nodes)}, 送中 {unlock['songzhong']})",
            flush=True,
        )


def start_fleet_report_scheduler(db: MasterDB, tg: TelegramAPI) -> threading.Thread:
    """Master 内置调度：UTC 16:30 自动全节点总结."""
    import time

    def _loop() -> None:
        last_run: datetime.date | None = None
        while True:
            time.sleep(30)
            now = datetime.now(timezone.utc)
            if now.hour != 16 or now.minute != 30:
                continue
            if last_run == now.date():
                continue
            last_run = now.date()
            cfg = load_master_config()
            if not cfg.get("TG_TOKEN"):
                continue
            try:
                run_fleet_daily_report(cfg, db, tg)
            except Exception as exc:
                print(f"[ip-sentinel-master] 全节点总结异常: {exc}", flush=True)

    t = threading.Thread(target=_loop, daemon=True, name="master-fleet-report")
    t.start()
    return t
