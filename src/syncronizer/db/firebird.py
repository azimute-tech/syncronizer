"""Firebird 2.5 source client (pure-Python ``firebirdsql`` driver).

Opened per cycle. A connection/availability failure raises
:class:`FirebirdUnavailable` so the orchestrator can log + skip the ETL phase
without crashing the always-on service. Per-query errors (bad SQL, missing table)
propagate normally and are isolated per-endpoint by the orchestrator.
"""
from __future__ import annotations

from typing import Iterator, Sequence

import firebirdsql

from ..core.types import normalize

_FETCH_BATCH = 500


class FirebirdUnavailable(RuntimeError):
    """The Firebird server/database could not be reached or opened."""


class FirebirdClient:
    def __init__(self, settings, log=None):
        self.s = settings
        self.log = log
        self._conn = None

    def connect(self) -> "FirebirdClient":
        path = self.s.require_firebird_path()
        kwargs = dict(
            host=self.s.firebird_host,
            database=str(path),
            port=self.s.firebird_port,
            user=self.s.firebird_user,
            password=self.s.firebird_password,
            charset=self.s.firebird_charset,
        )
        try:
            try:
                self._conn = firebirdsql.connect(
                    timeout=self.s.firebird_connect_timeout, **kwargs
                )
            except TypeError:
                # older firebirdsql without a `timeout` kwarg
                self._conn = firebirdsql.connect(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize to FirebirdUnavailable
            raise FirebirdUnavailable(
                f"cannot connect to Firebird {self.s.firebird_host}:{self.s.firebird_port} "
                f"db={path}: {exc}"
            ) from exc
        return self

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def fetch(self, sql: str, params: Sequence = ()) -> Iterator[dict]:
        """Run a query and yield normalized ``{COLUMN: value}`` dicts.

        Firebird upcases unquoted identifiers, so dict keys are the column names
        as Firebird returns them (typically UPPER). Values are passed through
        :func:`syncronizer.core.types.normalize`.
        """
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description] if cur.description else []
            while True:
                rows = cur.fetchmany(_FETCH_BATCH)
                if not rows:
                    break
                for row in rows:
                    yield {col: normalize(val) for col, val in zip(cols, row)}
        finally:
            cur.close()
            # Release the read-committed snapshot (OAT hygiene), even if iteration
            # was abandoned by a consumer-side exception (GeneratorExit).
            try:
                self._conn.commit()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "FirebirdClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
