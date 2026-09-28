"""SQLite LedgerBackend used for local execution and real-DB boundary tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, TypeVar


_T = TypeVar("_T")


class SQLiteLedgerBackend:
    """Scope-bound JSON record store with an explicit batch transaction seam."""

    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS ledger_records ("
                "record_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, record_json TEXT NOT NULL)"
            )

    def insert(self, record: Mapping[str, Any]) -> str:
        record_id = record.get("record_id")
        scope = record.get("scope")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("ledger_backend_record_id_required")
        if not isinstance(scope, Mapping):
            raise ValueError("ledger_backend_scope_required")
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT INTO ledger_records(record_id, scope_json, record_json) VALUES (?, ?, ?)",
                    (record_id, _scope_json(scope), _record_json(record)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("ledger_backend_record_id_conflict") from exc
        return record_id

    def get(self, record_id: str, *, scope: Mapping[str, str]) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record_json FROM ledger_records WHERE record_id = ? AND scope_json = ?",
                (record_id, _scope_json(scope)),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def update(
        self,
        record_id: str,
        *,
        scope: Mapping[str, str],
        changes: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._lock:
            current = self.get(record_id, scope=scope)
            if current is None:
                return None
            current.update(dict(changes))
            self._connection.execute(
                "UPDATE ledger_records SET record_json = ? WHERE record_id = ? AND scope_json = ?",
                (_record_json(current), record_id, _scope_json(scope)),
            )
            return dict(current)

    def query(self, *, scope: Mapping[str, str]) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT record_json FROM ledger_records WHERE scope_json = ? ORDER BY record_id",
                (_scope_json(scope),),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def purge_raw(self, *, scope: Mapping[str, str], cutoff: str) -> list[str]:
        cutoff_at = _parse_instant(cutoff)
        purged: list[str] = []
        with self._lock:
            for record in self.query(scope=scope):
                recorded_at = record.get("recorded_at")
                if not isinstance(recorded_at, str) or _parse_instant(recorded_at) >= cutoff_at:
                    continue
                raw_keys = [key for key in record if key.endswith("_raw")]
                if not any(record.get(key) is not None for key in raw_keys):
                    continue
                for key in raw_keys:
                    record[key] = None
                self._connection.execute(
                    "UPDATE ledger_records SET record_json = ? WHERE record_id = ? AND scope_json = ?",
                    (_record_json(record), record["record_id"], _scope_json(scope)),
                )
                purged.append(record["record_id"])
        return sorted(purged)

    def run_atomically(self, operation: Callable[[], _T]) -> _T:
        """Run ledger writes in one SQLite transaction, rolling all back on error."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation()
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
            return result


def _scope_json(scope: Mapping[str, Any]) -> str:
    return json.dumps(dict(scope), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _record_json(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ledger_backend_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("ledger_backend_timestamp_timezone_required")
    return parsed
