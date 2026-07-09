"""Master 长轮询主循环."""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Any

from master.agent_client import bind_ws_hub
from master.config import require_master_config
from master.db import MasterDB
from master.handlers import MasterHandlers
from master.telegram_api import TelegramAPI
from master.ws_server import AgentWSHub
from wss_constants import MASTER_WSS_BIND, MASTER_WSS_PORT


def _extract_update(
    update: dict[str, Any],
) -> tuple[str, str, str | None, int | None, int | None, int | None, str]:
    msg = update.get("message") or {}
    cb = update.get("callback_query") or {}
    cb_msg = cb.get("message") or {}

    chat_id = str(msg.get("chat", {}).get("id") or cb_msg.get("chat", {}).get("id") or "")
    text = (msg.get("text") or cb.get("data") or "").strip()
    cb_id = cb.get("id")
    msg_id = cb_msg.get("message_id")
    user_msg_id = msg.get("message_id")
    thread_raw = msg.get("message_thread_id") or cb_msg.get("message_thread_id")
    thread_id = int(thread_raw) if thread_raw else None
    reply_to = (msg.get("reply_to_message") or {}).get("text") or ""
    return chat_id, text, cb_id, msg_id, thread_id, user_msg_id, reply_to


def run() -> None:
    cfg = require_master_config()
    master_dir = cfg["MASTER_DIR"]
    offset_file = Path(master_dir) / ".tg_offset"
    if not offset_file.exists():
        offset_file.write_text("0", encoding="utf-8")

    db = MasterDB(cfg["DB_FILE"])
    tg = TelegramAPI(cfg["TG_TOKEN"])
    ws_hub = AgentWSHub(db, master_dir=master_dir)
    ws_hub.start()
    bind_ws_hub(ws_hub)
    handlers = MasterHandlers(cfg, db, tg)

    print(f"[ip-sentinel-master] 长轮询已启动 | WSS {MASTER_WSS_BIND}:{MASTER_WSS_PORT}", flush=True)

    while True:
        try:
            offset = int(offset_file.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            offset = 0

        updates = tg.get_updates(offset, timeout=30)
        for update in updates:
            uid = update.get("update_id", 0)
            next_offset = uid + 1

            chat_id, text, cb_id, msg_id, thread_id, user_msg_id, reply_to = _extract_update(update)
            if not chat_id:
                offset_file.write_text(str(next_offset), encoding="utf-8")
                continue

            try:
                handlers.dispatch(
                    chat_id,
                    text,
                    cb_id=cb_id,
                    msg_id=msg_id,
                    thread_id=thread_id,
                    user_msg_id=user_msg_id,
                    reply_to_text=reply_to,
                )
            except Exception:
                print(
                    f"[ip-sentinel-master] 处理 update {uid} 异常:\n{traceback.format_exc()}",
                    file=sys.stderr,
                    flush=True,
                )
                if chat_id:
                    tg.send_message(
                        chat_id,
                        "⚠️ 处理请求时发生内部错误，请稍后重试或发送 /start。",
                        markdown=False,
                    )

            offset_file.write_text(str(next_offset), encoding="utf-8")

        time.sleep(1)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
