"""Worked EXAMPLE endpoint (inert).

Demonstrates the full shape a real endpoint follows. The extract uses ``FIRST 0`` so
it returns no rows — discovery, ETL and SEND wiring are exercised without touching
production data. Replace ``_BASE_SQL`` and ``transform`` to make it real.
"""
from __future__ import annotations

from syncronizer.core.endpoint import Endpoint
from syncronizer.core.extract import ExtractContext, ExtractSpec


class ProdutosEndpoint(Endpoint):
    name = "produtos"                # -> control table ep_produtos
    primary_key = "id"
    order = 10
    api_path = "/produtos"
    api_method = "POST"
    incremental_column = None        # set to e.g. "DATA_ALTERACAO" for incremental
    reconcile_deletes = False

    # Inert until a real query is written: FIRST 0 returns zero rows.
    _BASE_SQL = "SELECT FIRST 0 ID, DESCRICAO, PRECO FROM PRODUTOS"

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        if not self.incremental_column or ctx.last_watermark is None:
            return ExtractSpec(sql=self._BASE_SQL, params=())
        sql = f"{self._BASE_SQL} WHERE {self.incremental_column} > ?"
        return ExtractSpec(sql=sql, params=(ctx.last_watermark,), incremental=True)

    def transform(self, row: dict) -> dict:
        # Firebird returns UPPER-case column names for unquoted identifiers.
        return {
            "id": row["ID"],
            "descricao": row["DESCRICAO"],
            "preco": float(row["PRECO"]) if row["PRECO"] is not None else None,
        }
