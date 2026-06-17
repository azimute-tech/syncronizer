"""Per-value normalization of raw Firebird column values.

Kept tiny and side-effect free so it is unit-testable with no live database.
"""
from __future__ import annotations


def normalize(value):
    """Normalize one Firebird value for transform/hashing/storage.

    - ``None`` passes through.
    - ``str`` is right-stripped of spaces: Firebird ``CHAR(n)`` is space-padded,
      and without this the computed ``row_hash`` would never be stable.
    - ``bytes`` is decoded as UTF-8 (text BLOB sub_type 1). Binary BLOBs would need
      a dedicated per-endpoint branch; do not assume binary data is text.
    - ``Decimal`` / ``date`` / ``datetime`` / ``int`` / ``float`` pass through; the
      hashing/JSON layer renders them deterministically.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.rstrip(" ")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value
