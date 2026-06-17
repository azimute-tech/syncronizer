"""TEMPLATE — copy me to create an endpoint.

To create endpoint X:
  1. Copy this file to ``endpoints/<x>.py``  (NO leading underscore, or the registry skips it).
  2. Rename the class; set ``name`` / ``primary_key`` / ``api_path``.
  3. Replace the inert ``SELECT FIRST 0 ...`` with the real Firebird 2.5 SQL
     (use FIRST/SKIP, not LIMIT; use qmark ``?`` placeholders, not ``:named``).
  4. Map the raw UPPER-case Firebird columns to the payload dict in ``transform()``.
  5. (optional) set ``incremental_column`` to a source timestamp/sequence for delta
     extracts; leave None for a full scan diffed by row_hash.
  6. (optional) set ``reconcile_deletes = True`` to tombstone rows that vanish from
     the source (full-scan only). Override ``build_payload`` / ``send`` if needed.

The base owns the Firebird connection, transaction, normalization, change detection
(row_hash), the sent/deleted flags, retries and HTTP — you only declare metadata and
``transform()``.

This module is underscore-prefixed so the registry never loads it.
"""
from __future__ import annotations

from syncronizer.core.endpoint import Endpoint
from syncronizer.core.extract import ExtractContext, ExtractSpec


class _TemplateEndpoint(Endpoint):
    name = "TODO_name"               # -> control table ep_TODO_name  (lowercase/digits/_)
    primary_key = "id"               # field in the TRANSFORMED record; tuple for composite keys
    order = 100
    api_path = "/TODO"
    api_method = "POST"
    incremental_column = None        # e.g. "DATA_ALTERACAO"; None = full scan + row_hash
    reconcile_deletes = False        # tombstone deleted rows (full scan only)

    _BASE_SQL = "SELECT FIRST 0 ID FROM TODO_TABLE"  # inert until you write the real query

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        if not self.incremental_column or ctx.last_watermark is None:
            return ExtractSpec(sql=self._BASE_SQL, params=())
        sql = f"{self._BASE_SQL} WHERE {self.incremental_column} > ?"
        return ExtractSpec(sql=sql, params=(ctx.last_watermark,), incremental=True)

    def transform(self, row: dict) -> dict:
        return {"id": row["ID"]}
