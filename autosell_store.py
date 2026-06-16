"""
SQLite storage for Auto-Sell runtime data.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_FILE = Path(__file__).resolve().with_name("autosell.sqlite3")


def _now_ts() -> int:
    return int(time.time())


class AutoSellStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DB_FILE
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS purchases (
                    gift_id TEXT PRIMARY KEY,
                    buy_price REAL NOT NULL,
                    collection_name TEXT NOT NULL,
                    collection_title TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    model_rarity_percent REAL,
                    bought_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sell_lots (
                    gift_id TEXT PRIMARY KEY,
                    buy_price REAL NOT NULL,
                    collection_name TEXT NOT NULL,
                    collection_title TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    model_rarity_percent REAL,
                    listed_price REAL,
                    first_listed_at INTEGER,
                    last_action_at INTEGER,
                    next_critical_at INTEGER,
                    relist_count INTEGER NOT NULL DEFAULT 0,
                    extra_changes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    rare_flag INTEGER NOT NULL DEFAULT 0,
                    last_floor REAL,
                    last_order_bid REAL,
                    last_error TEXT,
                    sold_price REAL,
                    sold_at INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS critical_prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gift_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    deadline INTEGER NOT NULL,
                    options_json TEXT NOT NULL,
                    default_on_timeout TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    resolved_action TEXT,
                    resolved_by TEXT,
                    resolved_at INTEGER,
                    handled_by_worker INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sell_actions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gift_id TEXT,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sell_lots_status
                    ON sell_lots(status);
                CREATE INDEX IF NOT EXISTS idx_critical_prompts_status_deadline
                    ON critical_prompts(status, deadline);
                CREATE INDEX IF NOT EXISTS idx_critical_prompts_handled
                    ON critical_prompts(status, handled_by_worker);
                """
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def log_action(
        self,
        gift_id: str | None,
        action: str,
        source: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sell_actions_log(gift_id, action, source, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    gift_id,
                    action,
                    source,
                    json.dumps(payload or {}, ensure_ascii=True),
                    _now_ts(),
                ),
            )

    def record_purchase(
        self,
        *,
        gift_id: str,
        buy_price_ton: float,
        collection_name: str,
        collection_title: str,
        model_name: str = "",
        model_rarity_percent: float | None = None,
    ) -> None:
        now_ts = _now_ts()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO purchases(
                    gift_id, buy_price, collection_name, collection_title,
                    model_name, model_rarity_percent, bought_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gift_id,
                    float(buy_price_ton),
                    collection_name,
                    collection_title,
                    model_name or "",
                    model_rarity_percent,
                    now_ts,
                ),
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO sell_lots(
                    gift_id, buy_price, collection_name, collection_title,
                    model_name, model_rarity_percent, status, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 'NEW', ?)
                """,
                (
                    gift_id,
                    float(buy_price_ton),
                    collection_name,
                    collection_title,
                    model_name or "",
                    model_rarity_percent,
                    now_ts,
                ),
            )

    def list_lots(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sell_lots"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY updated_at ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def get_lot(self, gift_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sell_lots WHERE gift_id = ?",
                (gift_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def update_lot(self, gift_id: str, **fields: Any) -> None:
        if not fields:
            return

        fields["updated_at"] = _now_ts()
        keys = list(fields.keys())
        set_clause = ", ".join(f"{key} = ?" for key in keys)
        params = [fields[key] for key in keys]
        params.append(gift_id)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE sell_lots SET {set_clause} WHERE gift_id = ?",
                params,
            )

    def mark_sold(self, gift_id: str, sold_price_ton: float | None = None) -> None:
        self.update_lot(
            gift_id,
            status="SOLD",
            sold_price=sold_price_ton,
            sold_at=_now_ts(),
        )

    def create_critical_prompt(
        self,
        *,
        gift_id: str,
        reason: str,
        deadline_ts: int,
        options: list[str],
        default_on_timeout: str = "hold",
    ) -> int:
        existing = self.get_open_prompt_for_gift(gift_id)
        if existing:
            return int(existing["id"])

        now_ts = _now_ts()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO critical_prompts(
                    gift_id, reason, created_at, deadline, options_json, default_on_timeout, status
                )
                VALUES(?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (
                    gift_id,
                    reason,
                    now_ts,
                    int(deadline_ts),
                    json.dumps(options, ensure_ascii=True),
                    default_on_timeout,
                ),
            )
            prompt_id = int(cursor.lastrowid)
        return prompt_id

    def get_prompt(self, prompt_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM critical_prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def get_open_prompt_for_gift(self, gift_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM critical_prompts
                WHERE gift_id = ? AND status = 'OPEN'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (gift_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def list_open_prompts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM critical_prompts WHERE status = 'OPEN' ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def list_expired_open_prompts(self, now_ts: int | None = None) -> list[dict[str, Any]]:
        ts = int(now_ts or _now_ts())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM critical_prompts
                WHERE status = 'OPEN' AND deadline <= ?
                ORDER BY deadline ASC
                """,
                (ts,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def resolve_prompt(self, prompt_id: int, *, action: str, source: str) -> None:
        now_ts = _now_ts()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE critical_prompts
                SET status = 'CLOSED',
                    resolved_action = ?,
                    resolved_by = ?,
                    resolved_at = ?,
                    handled_by_worker = 0
                WHERE id = ? AND status = 'OPEN'
                """,
                (action, source, now_ts, prompt_id),
            )

    def list_unhandled_resolved_prompts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM critical_prompts
                WHERE status = 'CLOSED' AND handled_by_worker = 0
                ORDER BY resolved_at ASC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def mark_prompt_handled(self, prompt_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE critical_prompts SET handled_by_worker = 1 WHERE id = ?",
                (prompt_id,),
            )

    def list_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sell_actions_log
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def list_recent_purchase_ids(self, limit: int = 20) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT gift_id
                FROM purchases
                ORDER BY bought_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [str(row["gift_id"]) for row in rows if row and row["gift_id"]]

    def list_recent_purchases(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT gift_id, buy_price, collection_name, collection_title, model_name, model_rarity_percent, bought_at
                FROM purchases
                ORDER BY bought_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def get_purchase(self, gift_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT gift_id, buy_price, collection_name, collection_title, model_name, model_rarity_percent, bought_at
                FROM purchases
                WHERE gift_id = ?
                LIMIT 1
                """,
                (gift_id,),
            ).fetchone()
        return self._row_to_dict(row)
