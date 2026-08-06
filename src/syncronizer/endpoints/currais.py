"""TGC `currais` endpoint — sync pens/paddocks from Firebird (CAD_CURRAL) to AgroDB.

Source: Firebird 2.5 `CAD_CURRAL`.
Target: POST /api/integracoes/tgc/currais  body {"currais": [ ... ]}.
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

Runs first (order=10) because both `lotes` and `animais` reference a curral. The table
is tiny (19 rows in the client database), so it is a full scan every cycle and
``row_hash`` decides what actually gets re-sent.

`CC_TIPO` is what separates a paddock from a feedlot pen (PASTO | CONFINAMENTO) — it is
the field AgroDB needs to place head in pasto vs confinamento. `CC_HOSPITAL` is the
CHAR(1) 'S'/'N' flag for the infirmary pen and is normalized to a real boolean here.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    flag_sn,
    integer,
    num,
    opt_str,
    req_str,
)


class CurraisEndpoint(BatchEndpoint):
    name = "currais"                 # -> control table ep_currais
    primary_key = "COD_CURRAL"       # unique within a farm (one syncronizer = one farm/token)
    order = 10
    api_path = "/api/integracoes/tgc/currais"
    api_method = "POST"

    # Full scan + row_hash change detection; the table has tens of rows, not millions.
    incremental_column = None
    # A curral is never deleted in TGC (it is set inactive via CC_STATUS), so there is
    # nothing to tombstone; deactivation travels in the payload as STATUS.
    reconcile_deletes = False

    payload_key = "currais"
    record_key = "COD_CURRAL"
    error_key = "cod_curral"

    _BASE_SQL = """
        SELECT
            c.CC_CODIGO   AS COD_CURRAL,
            c.CC_NOME     AS NOME,
            c.CC_TIPO     AS TIPO,
            c.CC_HOSPITAL AS IS_HOSPITAL,
            c.CC_LOTAMAX  AS LOTACAO_MAXIMA,
            c.CC_LOTAMIN  AS LOTACAO_MINIMA,
            c.CC_AREA     AS AREA_HA,
            c.CC_SETOR    AS SETOR,
            c.CC_STATUS   AS STATUS
        FROM CAD_CURRAL c
        ORDER BY c.CC_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_CURRAL": req_str(row.get("COD_CURRAL")),
            "NOME": req_str(row.get("NOME")),
            "TIPO": opt_str(row.get("TIPO")),          # PASTO | CONFINAMENTO
            "IS_HOSPITAL": flag_sn(row.get("IS_HOSPITAL")),
            "LOTACAO_MAXIMA": integer(row.get("LOTACAO_MAXIMA")),
            "LOTACAO_MINIMA": integer(row.get("LOTACAO_MINIMA")),
            "AREA_HA": num(row.get("AREA_HA")),
            "SETOR": opt_str(row.get("SETOR")),
            "STATUS": opt_str(row.get("STATUS")),
        }
