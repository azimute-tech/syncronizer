"""Forward-only control-DB migrations, run by the boot gate before the scheduler.

The base meta tables are created by :meth:`ControlStore._bootstrap`. Each entry in
:data:`MIGRATIONS` upgrades the DB from version ``N-1`` to ``N``. v1 is the initial
schema, so there are no upgrade steps yet — add them here as the schema evolves.
"""
from __future__ import annotations

from .. import CODE_SCHEMA_VERSION

# version -> callable(sqlite3.Connection) applying the upgrade to that version.
MIGRATIONS = {}  # type: dict


def run_migrations(store, log=None) -> int:
    """Bring the control DB up to ``CODE_SCHEMA_VERSION``; refuse a downgrade."""
    current = store.schema_version
    if current is None:
        # Fresh DB: bootstrap already created the current schema.
        store.set_schema_version(CODE_SCHEMA_VERSION)
        if log:
            log.info("control.db initialized at schema_version=%d", CODE_SCHEMA_VERSION)
        return CODE_SCHEMA_VERSION

    if current > CODE_SCHEMA_VERSION:
        raise RuntimeError(
            f"control.db schema_version {current} is newer than code "
            f"{CODE_SCHEMA_VERSION}; refusing to start (code was rolled back). "
            "Roll the code forward or restore a compatible control.db."
        )

    while current < CODE_SCHEMA_VERSION:
        target = current + 1
        step = MIGRATIONS.get(target)
        if step is not None:
            step(store.conn)
            store.conn.commit()
        store.set_schema_version(target)
        if log:
            log.info("migrated control.db schema %d -> %d", current, target)
        current = target

    return current
