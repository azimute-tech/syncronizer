"""The Endpoint contract — one module per feed owns BOTH ETL and SEND.

Subclasses declare metadata (name, primary_key, api_path, incremental_column,
reconcile_deletes) and implement ``extract_spec`` + ``transform``. The base
provides ``make_pk``, ``build_payload`` and a default ``send`` (one POST per row).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence, Tuple, Union

from .extract import ExtractContext, ExtractSpec


class SendResult:
    """Outcome of sending a batch: ``ok`` pks and ``failed`` (pk, error) pairs."""

    def __init__(self, ok: Optional[Sequence[str]] = None,
                 failed: Optional[Sequence[Tuple[str, str]]] = None):
        self.ok: List[str] = list(ok or [])
        self.failed: List[Tuple[str, str]] = list(failed or [])


class Endpoint(ABC):
    # --- identity / ordering ---
    name: str = ""
    primary_key: Union[str, Sequence[str]] = "id"
    order: int = 100

    # --- SEND metadata ---
    api_path: str = ""
    api_method: str = "POST"

    # --- ETL options ---
    incremental_column: Optional[str] = None  # raw Firebird column; None = full scan
    reconcile_deletes: bool = False           # opt-in tombstoning (full scan only)

    # --- ETL hooks ---
    @abstractmethod
    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        """Return the Firebird query (qmark ``?`` placeholders) to extract rows."""

    @abstractmethod
    def transform(self, row: dict) -> dict:
        """Map a raw row (UPPER-case Firebird columns) to a business record.

        Return ONLY business fields; the base attaches timestamps and computes the
        row hash. The returned dict is what gets stored and (by default) POSTed.
        """

    def make_pk(self, record: dict) -> str:
        """Derive the control-table primary key from the transformed record."""
        if isinstance(self.primary_key, (list, tuple)):
            return "|".join(str(record[k]) for k in self.primary_key)
        return str(record[self.primary_key])

    # --- SEND hooks ---
    def build_payload(self, record: dict) -> dict:
        """Shape the API body from the stored record. Default: identity."""
        return record

    def delete_path(self, pk: str) -> str:
        """URL path used to delete a tombstoned row. Default ``{api_path}/{pk}``.

        Override if your API expresses deletes differently (e.g. a soft-delete POST).
        """
        return f"{self.api_path.rstrip('/')}/{pk}"

    def send(self, http, rows: Sequence[dict]) -> SendResult:
        """Default send: one request per unsent row.

        ``rows`` items are ``{"pk", "payload"(json str), "deleted"(bool)}``. A row with
        ``deleted=True`` (tombstoned by reconcile) issues an HTTP DELETE to
        ``delete_path(pk)`` instead of re-POSTing the stale payload — so a deletion is
        never silently masked as an upsert. Override for batch/array APIs.
        """
        ok: List[str] = []
        failed: List[Tuple[str, str]] = []
        for row in rows:
            pk = row["pk"]
            try:
                if row.get("deleted"):
                    http.request("DELETE", self.delete_path(pk))
                else:
                    record = json.loads(row["payload"])
                    payload = self.build_payload(record)
                    http.request(self.api_method, self.api_path, json=payload)
                ok.append(pk)
            except Exception as exc:  # noqa: BLE001 - per-row isolation
                failed.append((pk, str(exc)))
        return SendResult(ok, failed)
