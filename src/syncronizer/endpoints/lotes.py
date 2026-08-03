"""TGC `lotes` endpoint — sync lots from Firebird (CAD_LOTE) to AgroDB.

Source: Firebird 2.5 `CAD_LOTE`.
Target: POST /api/integracoes/tgc/lotes  body {"lotes": [ ... ]}.
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

Runs after `currais` (order=20) because a lote points at a curral, and before `animais`,
which points at both. Full scan every cycle (a farm has tens of lots) with ``row_hash``
deciding what is actually re-sent.

CAREFUL — the phase of the lot is `CLL_TIPO_EXPLORACAO` (RECRIA | TIP |
SEMICONFINAMENTO | CONFINAMENTO), NOT `CLL_TIPO`. `CLL_TIPO` is the ownership regime
("PRÓPRIO" for every row in the client database) and says nothing about pasto vs
confinamento; reading it as the phase would classify the whole herd wrong.

`CLL_STATUS` is forwarded raw as TGC stores it — CHAR(1), 'A' (ativo) / 'I' (inativo).
It is deliberately NOT expanded to ATIVO/INATIVO here: inventing labels in the agent
would hide what the source actually holds.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    integer,
    iso_date,
    opt_code,
    opt_str,
    req_str,
)


class LotesEndpoint(BatchEndpoint):
    name = "lotes"                   # -> control table ep_lotes
    primary_key = "COD_LOTE"         # unique within a farm (one syncronizer = one farm/token)
    order = 20
    api_path = "/api/integracoes/tgc/lotes"
    api_method = "POST"

    # Full scan + row_hash change detection.
    incremental_column = None
    # A lote is closed (CLL_STATUS = 'I', CLL_DATAFIM), never deleted — closure travels
    # in the payload, so there is nothing to tombstone.
    reconcile_deletes = False

    payload_key = "lotes"
    record_key = "COD_LOTE"
    error_key = "cod_lote"

    _BASE_SQL = """
        SELECT
            l.CLL_CODNOME         AS COD_LOTE,
            l.CLL_CODCURRAL       AS COD_CURRAL,
            l.CLL_TIPO_EXPLORACAO AS TIPO_EXPLORACAO,
            l.CLL_STATUS          AS STATUS,
            l.CLL_QTDECAB         AS QTD_CABECAS,
            l.CLL_DIASCONFINAM    AS DIAS_CONFINAMENTO_ALVO,
            l.CLL_DATA_MEDIA_ENT  AS DATA_MEDIA_ENTRADA,
            l.CLL_DATAFIM         AS DATA_FIM,
            l.CLL_NUMCONTRATO     AS NUM_CONTRATO
        FROM CAD_LOTE l
        ORDER BY l.CLL_CODNOME
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_LOTE": req_str(row.get("COD_LOTE")),
            # 0 = TGC's "no curral" sentinel on a closed lote; there is no curral 0
            "COD_CURRAL": opt_code(row.get("COD_CURRAL")),
            # the PHASE of the lot — CLL_TIPO_EXPLORACAO, never CLL_TIPO
            "TIPO_EXPLORACAO": opt_str(row.get("TIPO_EXPLORACAO")),
            "STATUS": opt_str(row.get("STATUS")),          # raw TGC CHAR(1): 'A' | 'I'
            "QTD_CABECAS": integer(row.get("QTD_CABECAS")),
            "DIAS_CONFINAMENTO_ALVO": integer(row.get("DIAS_CONFINAMENTO_ALVO")),
            "DATA_MEDIA_ENTRADA": iso_date(row.get("DATA_MEDIA_ENTRADA")),
            "DATA_FIM": iso_date(row.get("DATA_FIM")),
            "NUM_CONTRATO": opt_str(row.get("NUM_CONTRATO")),
        }
