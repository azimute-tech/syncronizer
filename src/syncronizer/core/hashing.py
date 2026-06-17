"""Stable content hashing for change detection.

``row_hash`` is the single source of truth used by both ETL (to decide new/changed)
and the control store. It MUST be computed over the business record only — never
over timestamps or the sent/deleted flags, which would cause endless churn.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal


def _encode(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        # Aware -> UTC ISO; naive (typical for Firebird) -> ISO as-is (deterministic).
        if obj.tzinfo is not None:
            return obj.astimezone(timezone.utc).isoformat()
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    raise TypeError(f"cannot serialize value of type {type(obj).__name__}")


def canonical_json(record: dict) -> str:
    """Deterministic JSON: sorted keys, compact separators, custom encoder.

    Also the exact bytes stored as the row payload, so storage and hashing agree.
    """
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_encode,
    )


def row_hash(record: dict) -> str:
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
