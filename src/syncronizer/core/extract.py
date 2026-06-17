"""Extraction spec returned by an endpoint and the context passed to it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ExtractSpec:
    """A Firebird query to run.

    ``sql`` MUST use qmark ``?`` placeholders (firebirdsql ``paramstyle == 'qmark'``);
    ``:named`` binds are NOT supported. ``params`` is the positional tuple.
    """

    sql: str
    params: Tuple = ()
    incremental: bool = False


@dataclass(frozen=True)
class ExtractContext:
    """State handed to ``Endpoint.extract_spec`` (e.g. the last watermark)."""

    last_watermark: Optional[str] = None
