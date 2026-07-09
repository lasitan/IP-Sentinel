"""SQLite WAL 数据库访问 (参数化查询)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class MasterDB:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _migrate(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    chat_id TEXT,
                    node_name TEXT,
                    agent_ip TEXT,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    region TEXT DEFAULT 'UNKNOWN',
                    node_alias TEXT,
                    enable_google TEXT DEFAULT 'true',
                    enable_trust TEXT DEFAULT 'true',
                    enable_ota TEXT DEFAULT 'false',
                    PRIMARY KEY(chat_id, node_name)
                );
                CREATE TABLE IF NOT EXISTS ip_trend_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_name TEXT,
                    check_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scam_score INTEGER,
                    goog_status TEXT,
                    nf_status TEXT,
                    gpt_status TEXT
                );
                """
            )
            for _col, ddl in (
                ("region", "ALTER TABLE nodes ADD COLUMN region TEXT DEFAULT 'UNKNOWN'"),
                ("node_alias", "ALTER TABLE nodes ADD COLUMN node_alias TEXT"),
                ("enable_google", "ALTER TABLE nodes ADD COLUMN enable_google TEXT DEFAULT 'true'"),
                ("enable_trust", "ALTER TABLE nodes ADD COLUMN enable_trust TEXT DEFAULT 'true'"),
                ("enable_ota", "ALTER TABLE nodes ADD COLUMN enable_ota TEXT DEFAULT 'false'"),
                ("message_thread_id", "ALTER TABLE nodes ADD COLUMN message_thread_id INTEGER"),
                ("topic_ui_message_id", "ALTER TABLE nodes ADD COLUMN topic_ui_message_id INTEGER"),
                ("topic_ui_edit_count", "ALTER TABLE nodes ADD COLUMN topic_ui_edit_count INTEGER DEFAULT 0"),
                ("goog_status", "ALTER TABLE ip_trend_log ADD COLUMN goog_status TEXT DEFAULT 'Unknown'"),
                ("gpt_status", "ALTER TABLE ip_trend_log ADD COLUMN gpt_status TEXT DEFAULT 'Unknown'"),
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            if cur.description:
                return cur.fetchall()
            return []

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self.execute(sql, params)
        if not rows:
            return None
        return rows[0][0]
