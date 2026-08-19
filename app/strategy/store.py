from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from app.strategy.base import Decision


@dataclass(frozen=True)
class DecisionRecord:
    id: int
    timestamp: str
    session_id: str
    strategy_name: str
    strategy_version: str
    action: str
    symbol: str | None
    reason_code: str
    reason: str
    signals: dict[str, str]
    executed: bool
    trade_id: int | None


class DecisionStore:
    def __init__(self, database_path: str | Path, *, max_records: int = 1000,
                 deduplicate_seconds: int = 30) -> None:
        if max_records <= 0 or deduplicate_seconds < 0:
            raise ValueError("decision log limits must be non-negative")
        self.database_path = Path(database_path)
        self.max_records = max_records
        self.deduplicate_seconds = deduplicate_seconds
        self._lock = RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS strategy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
                    strategy_name TEXT NOT NULL, strategy_version TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('BUY','SELL','HOLD','SKIP')),
                    symbol TEXT, reason_code TEXT NOT NULL, reason TEXT NOT NULL,
                    signals_json TEXT NOT NULL, executed INTEGER NOT NULL DEFAULT 0,
                    trade_id INTEGER
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, session_id: str, strategy_name: str, strategy_version: str,
               decision: Decision, *, executed: bool = False,
               trade_id: int | None = None, now: datetime | None = None) -> DecisionRecord | None:
        now = now or datetime.now(timezone.utc)
        signals_json = json.dumps(decision.signals, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM strategy_decisions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            recent_duplicate = (
                    previous
                    and datetime.fromisoformat(previous["timestamp"])
                    > now - timedelta(seconds=self.deduplicate_seconds)
            )
            if (not executed and decision.action in ("HOLD", "SKIP")
                    and recent_duplicate and not previous["executed"]
                    and previous["action"] == decision.action
                    and previous["symbol"] == decision.symbol
                    and previous["reason_code"] == decision.reason_code):
                return None
            cursor = connection.execute("""
                INSERT INTO strategy_decisions (
                    timestamp, session_id, strategy_name, strategy_version, action,
                    symbol, reason_code, reason, signals_json, executed, trade_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now.isoformat(), session_id, strategy_name, strategy_version,
                    decision.action, decision.symbol, decision.reason_code,
                    decision.reason, signals_json, int(executed), trade_id))
            overflow = connection.execute(
                "SELECT COUNT(*) - ? FROM strategy_decisions", (self.max_records,)
            ).fetchone()[0]
            if overflow > 0:
                connection.execute(
                    "DELETE FROM strategy_decisions WHERE id IN (SELECT id FROM strategy_decisions ORDER BY id LIMIT ?)",
                    (overflow,),
                )
            return DecisionRecord(int(cursor.lastrowid), now.isoformat(), session_id,
                                  strategy_name, strategy_version, decision.action,
                                  decision.symbol, decision.reason_code, decision.reason,
                                  decision.signals, executed, trade_id)

    def recent(self, session_id: str, limit: int = 20) -> list[DecisionRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_decisions WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [DecisionRecord(row["id"], row["timestamp"], row["session_id"],
                               row["strategy_name"], row["strategy_version"], row["action"],
                               row["symbol"], row["reason_code"], row["reason"],
                               json.loads(row["signals_json"]), bool(row["executed"]),
                               row["trade_id"]) for row in rows]
