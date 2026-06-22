"""Two-phase cycle: ETL (Firebird -> SQLite) then SEND (SQLite -> API).

Each endpoint runs in its own try/except in each phase, so one bad feed never
aborts the cycle. A Firebird-unavailable condition skips the ETL phase (the SEND
phase still flushes previously-staged rows).
"""
from __future__ import annotations

import collections
from typing import Sequence

from ..config import ConfigError
from ..db.firebird import FirebirdUnavailable
from ..db.store import now_iso
from ..runtime import CycleStats, EndpointStats
from .endpoint import Endpoint
from .extract import ExtractContext
from .hashing import canonical_json, row_hash


def run_cycle(endpoints: Sequence[Endpoint], fb, store, http, log, batch_size: int) -> CycleStats:
    stats = CycleStats()
    # only run endpoints that are enabled in the admin UI
    active = [ep for ep in endpoints if store.is_endpoint_enabled(ep.name)]

    # ---- PHASE 1: ETL ----
    try:
        fb.connect()
    except (FirebirdUnavailable, ConfigError) as exc:
        # Not reachable OR not configured yet (fresh install) — skip ETL, keep running
        # so the admin UI stays available for the user to configure it.
        stats.firebird_available = False
        log.warning("Firebird unavailable/not configured; skipping ETL this cycle: %s", exc)

    if stats.firebird_available:
        for ep in active:
            est = stats.endpoint(ep.name)
            try:
                _etl_endpoint(ep, fb, store, log, est)
            except FirebirdUnavailable as exc:
                log.warning("[%s] Firebird unavailable during ETL: %s", ep.name, exc)
                est.errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - isolate per endpoint
                log.exception("[%s] ETL failed: %s", ep.name, exc)
                est.errors.append(str(exc))
                store.log_run(ep.name, "etl", status="error", error=str(exc))

    # ---- PHASE 2: SEND ----
    for ep in active:
        est = stats.endpoint(ep.name)
        try:
            _send_endpoint(ep, store, http, log, est, batch_size)
        except Exception as exc:  # noqa: BLE001 - isolate per endpoint
            log.exception("[%s] SEND failed: %s", ep.name, exc)
            est.errors.append(str(exc))
            store.log_run(ep.name, "send", status="error", error=str(exc))

    return stats


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _watermark_max(current, candidate):
    """Return the larger watermark, numeric-aware.

    Watermarks round-trip through a TEXT column, so an integer/sequence column comes
    back as a string. Comparing it lexicographically would freeze ('10' < '9') or even
    regress the watermark; coerce both operands to numbers when possible.
    """
    if candidate is None:
        return current
    if current is None:
        return candidate
    cur_n, cand_n = _as_number(current), _as_number(candidate)
    if cur_n is not None and cand_n is not None:
        return candidate if cand_n > cur_n else current
    try:
        return candidate if candidate > current else current
    except TypeError:
        return candidate if str(candidate) > str(current) else current


def _etl_endpoint(ep: Endpoint, fb, store, log, est: EndpointStats) -> None:
    store.ensure_endpoint_table(ep.name)
    incremental = bool(ep.incremental_column)
    watermark = store.get_watermark(ep.name) if incremental else None
    spec = ep.extract_spec(ExtractContext(last_watermark=watermark))

    staged = []
    present_pks = []
    max_wm = watermark
    for row in fb.fetch(spec.sql, spec.params):
        est.extracted += 1
        record = ep.transform(row)
        pk = ep.make_pk(record)
        staged.append((pk, canonical_json(record), row_hash(record)))
        present_pks.append(pk)
        if incremental:
            max_wm = _watermark_max(max_wm, row.get(ep.incremental_column))

    counts = store.upsert_many(ep.name, staged)
    est.inserted = counts["inserted"]
    est.changed = counts["changed"]
    est.unchanged = counts["unchanged"]

    if ep.reconcile_deletes and not incremental:
        if present_pks:
            est.deleted = store.mark_missing_deleted(ep.name, present_pks)
        else:
            log.warning(
                "[%s] reconcile_deletes skipped: extract returned 0 rows "
                "(refusing to tombstone the whole table)", ep.name,
            )

    # str-normalize the comparison so an int max (10) vs a TEXT watermark ("10")
    # is not seen as different, which would rewrite the watermark every cycle.
    if incremental and max_wm is not None and str(max_wm) != str(watermark):
        store.set_watermark(ep.name, max_wm)

    store.log_run(
        ep.name, "etl", extracted=est.extracted, changed=est.changed,
        unchanged=est.unchanged, status="ok",
    )


def _send_endpoint(ep: Endpoint, store, http, log, est: EndpointStats, batch_size: int) -> None:
    store.ensure_endpoint_table(ep.name)
    rows = store.select_unsent(ep.name, batch_size)
    if not rows:
        return
    result = ep.send(http, rows)
    if result.ok:
        store.mark_sent(ep.name, result.ok)
        est.sent = len(result.ok)
    for pk, err in result.failed:
        store.mark_failed(ep.name, pk, err)
    est.failed = len(result.failed)
    if result.failed:
        reasons = collections.Counter(str(err)[:160] for _, err in result.failed)
        for reason, n in reasons.most_common(3):
            log.warning("[%s] envio falhou (%dx): %s", ep.name, n, reason)
    status = "ok" if not result.failed else "partial"
    store.touch_endpoint(ep.name, last_send_at=now_iso(), status=status)
    store.log_run(ep.name, "send", sent=est.sent, failed=est.failed, status=status)
