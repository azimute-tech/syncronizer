"""SQLite control store: owns the single long-lived connection.

The connection is created with ``check_same_thread=False`` because APScheduler runs
jobs on a worker thread, not the thread that built the store. Concurrency is still
avoided by construction (a single-worker executor + ``max_instances=1`` per job),
and an :class:`threading.RLock` guards every statement as belt-and-suspenders.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import schema

_IN_CHUNK = 900  # stay under SQLite's variable limit for IN(...) lists


def now_iso() -> str:
    """Current UTC time as ISO-8601 with offset."""
    return datetime.now(timezone.utc).isoformat()


def _chunks(seq: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class ControlStore:
    def __init__(self, control_db: Path, log=None):
        if sqlite3.sqlite_version_info < (3, 24, 0):
            raise RuntimeError(
                f"SQLite >= 3.24 required for UPSERT, found {sqlite3.sqlite_version}"
            )
        self.path = Path(control_db)
        self.log = log
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._bootstrap()

    # -- lifecycle ---------------------------------------------------------
    def _bootstrap(self) -> None:
        with self._lock:
            for pragma in schema.PRAGMAS:
                self.conn.execute(pragma)
            for ddl in schema.META_DDL:
                self.conn.execute(ddl)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    def checkpoint(self) -> None:
        """Keep the -wal file bounded for a long-running process."""
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    # -- meta / schema version --------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            cur = self.conn.execute("SELECT value FROM _sync_meta WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO _sync_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    @property
    def schema_version(self) -> Optional[int]:
        v = self.get_meta("schema_version")
        return int(v) if v is not None else None

    def set_schema_version(self, version: int) -> None:
        self.set_meta("schema_version", int(version))

    # -- endpoint tables ---------------------------------------------------
    def ensure_endpoint_table(self, name: str) -> str:
        table = schema.table_for(name)
        with self._lock, self.conn:
            self.conn.execute(schema.endpoint_table_ddl(table))
            self.conn.execute(schema.endpoint_index_ddl(table))
        return table

    def _existing(self, table: str, pks: Sequence[str]) -> Dict[str, Tuple[str, int]]:
        out: Dict[str, Tuple[str, int]] = {}
        with self._lock:
            for chunk in _chunks(list(pks), _IN_CHUNK):
                ph = ",".join("?" * len(chunk))
                cur = self.conn.execute(
                    f"SELECT pk, row_hash, deleted FROM {table} WHERE pk IN ({ph})",
                    list(chunk),
                )
                for pk, row_hash, deleted in cur.fetchall():
                    out[pk] = (row_hash, deleted)
        return out

    def upsert_many(self, name: str, staged: Sequence[Tuple[str, str, str]]) -> Dict[str, int]:
        """Apply (pk, payload_json, row_hash) rows. Returns {inserted, changed, unchanged}.

        New rows and changed rows (hash differs, or row was tombstoned) reset sent=0;
        unchanged rows are not touched (sent flag preserved).
        """
        table = schema.table_for(name)
        counts = {"inserted": 0, "changed": 0, "unchanged": 0}
        if not staged:
            return counts
        # De-duplicate by pk within the batch (last occurrence wins, matching the
        # last-write-wins UPSERT) so the counts reflect rows actually written.
        deduped = {}
        for row in staged:
            deduped[row[0]] = row
        staged = list(deduped.values())
        existing = self._existing(table, list(deduped.keys()))
        now = now_iso()
        to_apply: List[Tuple] = []
        for pk, payload, row_hash in staged:
            if pk not in existing:
                counts["inserted"] += 1
                to_apply.append((pk, payload, row_hash, now, now))
            else:
                old_hash, old_deleted = existing[pk]
                if old_hash != row_hash or old_deleted:
                    counts["changed"] += 1
                    to_apply.append((pk, payload, row_hash, now, now))
                else:
                    counts["unchanged"] += 1
        if to_apply:
            sql = schema.upsert_sql(table)
            with self._lock, self.conn:
                self.conn.executemany(sql, to_apply)
        return counts

    def mark_missing_deleted(self, name: str, present_pks: Iterable[str]) -> int:
        """Tombstone rows whose pk is no longer present in the source (full-scan only).

        Returns the number newly marked deleted. Callers MUST guarantee the source
        extract was a complete full scan; never call this for an incremental extract.
        """
        table = schema.table_for(name)
        now = now_iso()
        with self._lock, self.conn:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS _present(pk TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM _present")
            self.conn.executemany(
                "INSERT OR IGNORE INTO _present(pk) VALUES (?)",
                ((p,) for p in present_pks),
            )
            cur = self.conn.execute(
                f"UPDATE {table} SET deleted=1, sent=0, sent_at=NULL, updated_at=? "
                f"WHERE deleted=0 AND pk NOT IN (SELECT pk FROM _present)",
                (now,),
            )
            return cur.rowcount

    # -- send lifecycle ----------------------------------------------------
    def select_unsent(self, name: str, limit: int) -> List[dict]:
        table = schema.table_for(name)
        with self._lock:
            cur = self.conn.execute(
                f"SELECT pk, payload, deleted FROM {table} WHERE sent=0 "
                f"ORDER BY updated_at LIMIT ?",
                (int(limit),),
            )
            return [{"pk": r[0], "payload": r[1], "deleted": bool(r[2])} for r in cur.fetchall()]

    def mark_sent(self, name: str, pks: Sequence[str]) -> None:
        if not pks:
            return
        table = schema.table_for(name)
        now = now_iso()
        with self._lock, self.conn:
            for chunk in _chunks(list(pks), _IN_CHUNK):
                ph = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"UPDATE {table} SET sent=1, sent_at=?, last_error=NULL "
                    f"WHERE pk IN ({ph})",
                    [now, *chunk],
                )

    def mark_failed(self, name: str, pk: str, error) -> None:
        table = schema.table_for(name)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE {table} SET send_attempts = send_attempts + 1, last_error = ? "
                f"WHERE pk = ?",
                (str(error)[:1000], pk),
            )

    # -- endpoint state / watermark ---------------------------------------
    def get_watermark(self, endpoint: str) -> Optional[str]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT watermark FROM _endpoint_state WHERE endpoint=?", (endpoint,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_watermark(self, endpoint: str, watermark, etl_at: Optional[str] = None) -> None:
        now = etl_at or now_iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO _endpoint_state(endpoint, watermark, last_etl_at) "
                "VALUES(?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
                "watermark=excluded.watermark, last_etl_at=excluded.last_etl_at",
                (endpoint, None if watermark is None else str(watermark), now),
            )

    def touch_endpoint(self, endpoint: str, *, last_send_at: Optional[str] = None,
                       status: Optional[str] = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO _endpoint_state(endpoint, last_send_at, last_status) "
                "VALUES(?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
                "last_send_at=COALESCE(excluded.last_send_at, _endpoint_state.last_send_at), "
                "last_status=COALESCE(excluded.last_status, _endpoint_state.last_status)",
                (endpoint, last_send_at, status),
            )

    # -- run log -----------------------------------------------------------
    def log_run(self, endpoint: Optional[str], phase: str, **counts) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO _sync_log(run_at, endpoint, phase, extracted, changed, "
                "unchanged, sent, failed, status, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    now_iso(), endpoint, phase,
                    counts.get("extracted"), counts.get("changed"),
                    counts.get("unchanged"), counts.get("sent"), counts.get("failed"),
                    counts.get("status"), counts.get("error"),
                ),
            )
