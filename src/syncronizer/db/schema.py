"""SQLite schema: pragmas, meta-table DDL, per-endpoint table DDL, UPSERT template.

Pure SQL/strings + tiny builders. No connection logic lives here.
"""
from __future__ import annotations

import re

# Applied on every connection (a 24/7 service on a local disk).
PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA temp_store=MEMORY;",
)

# Control/meta tables.
META_DDL = (
    """
    CREATE TABLE IF NOT EXISTS _sync_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS _endpoint_state (
        endpoint     TEXT PRIMARY KEY,
        watermark    TEXT,
        last_etl_at  TEXT,
        last_send_at TEXT,
        last_status  TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS _endpoint_config (
        endpoint TEXT PRIMARY KEY,
        enabled  INTEGER NOT NULL DEFAULT 1
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS _sync_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at     TEXT NOT NULL,
        endpoint   TEXT,
        phase      TEXT,
        extracted  INTEGER,
        changed    INTEGER,
        unchanged  INTEGER,
        sent       INTEGER,
        failed     INTEGER,
        status     TEXT,
        error      TEXT
    );
    """,
)

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def table_for(name: str) -> str:
    """Validate an endpoint name and return its control table identifier.

    The identifier is interpolated into DDL/DML by f-string, so it MUST be
    validated against a strict allowlist (values are always bound, never names).
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid endpoint name {name!r}: only lowercase letters, digits and "
            "underscore are allowed"
        )
    return f"ep_{name}"


def endpoint_table_ddl(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        pk            TEXT PRIMARY KEY,
        payload       TEXT NOT NULL,
        row_hash      TEXT NOT NULL,
        sent          INTEGER NOT NULL DEFAULT 0,
        deleted       INTEGER NOT NULL DEFAULT 0,
        send_attempts INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        sent_at       TEXT
    );
    """


def endpoint_index_ddl(table: str) -> str:
    return (
        f"CREATE INDEX IF NOT EXISTS ix_{table}_unsent "
        f"ON {table}(sent) WHERE sent = 0;"
    )


def upsert_sql(table: str) -> str:
    """Guarded UPSERT: insert new rows (sent=0); on conflict update ONLY when the
    content changed (row_hash differs) or the row was previously tombstoned
    (deleted=1, i.e. resurrected) — resetting the sent flag. Unchanged rows are a
    no-op (rowcount 0) so their sent/sent_at are preserved.
    """
    return f"""
    INSERT INTO {table}
        (pk, payload, row_hash, sent, deleted, send_attempts, last_error,
         created_at, updated_at, sent_at)
    VALUES (?, ?, ?, 0, 0, 0, NULL, ?, ?, NULL)
    ON CONFLICT(pk) DO UPDATE SET
        payload       = excluded.payload,
        row_hash      = excluded.row_hash,
        sent          = 0,
        deleted       = 0,
        send_attempts = 0,
        last_error    = NULL,
        updated_at    = excluded.updated_at,
        sent_at       = NULL
    WHERE {table}.row_hash <> excluded.row_hash OR {table}.deleted = 1;
    """
