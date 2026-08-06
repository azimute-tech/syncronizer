"""TGC `movimentacoes` endpoint — sync physical movement history to AgroDB.

Source: Firebird 2.5 `CAD_HISTORICOTRASNF` (+ AUX_TIPO_MOV_FISICA).
Target: POST /api/integracoes/tgc/movimentacoes  body {"movimentacoes": [ ... ]}.
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

Runs last (order=40): the animal, the lote and the curral a movement points at have
already been sent this cycle.

INCREMENTAL — this is the first feed to use the watermark machinery. `AHT_CODIGO` is the
primary key AND the watermark: the table is an append-only event log fed by a monotonic
generator, so "id greater than the last one I saw" is a complete and cheap delta. The
first cycle has no watermark and reads the whole table (3.3k rows in the client
database); every cycle after that reads only the new events.

Two consequences of watermarking on the PK, both intended:
  * an UPDATE to an already-synced movement is NOT picked up (its id did not change).
    A movement is a historical fact — TGC appends a correcting event, it does not edit
    the old one.
  * ``reconcile_deletes`` stays False. It is only valid after a full scan; tombstoning
    from a delta would wipe every row not present in the last delta — i.e. everything.

`AHT_CURRALORIGEM` / `AHT_CURRALDESTINO` are VARCHAR(10) holding the curral NAME
(CC_NOME, e.g. "P1-1"), not the numeric code — the same key `animais.CURRAL_ATUAL`
carries, so the destination can join on it. The lote columns ARE numeric codes.

`AHT_IDMOVIMENTACAO` points at AUX_TIPO_MOV_FISICA, which splits the movement into a
direction (ATMF_STATUS: ENTRADA | SAIDA) and a reason (ATMF_TIPO: NASCIMENTO, PASTO,
COMPRA, MOVIMENTACAO, VENDA, MORTE, CONSUMO, REJEICAO, ANALISE_SITUACAO). The join is a
LEFT JOIN on purpose: an unknown id must still travel (with TIPO_ATMF filled and the two
labels null) so the destination sees the gap instead of the row vanishing.
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
    watermark_param,
)


class MovimentacoesEndpoint(BatchEndpoint):
    name = "movimentacoes"           # -> control table ep_movimentacoes
    primary_key = "COD_HISTORICO"    # unique within a farm (one syncronizer = one farm/token)
    order = 40
    api_path = "/api/integracoes/tgc/movimentacoes"
    api_method = "POST"

    # Delta by primary key; the orchestrator reads this RAW Firebird column off each
    # extracted row to advance the watermark, which is why _BASE_SQL selects
    # AHT_CODIGO un-aliased (an alias would make row.get("AHT_CODIGO") return None and
    # the watermark would never move).
    incremental_column = "AHT_CODIGO"
    reconcile_deletes = False        # invalid for an incremental extract — see module docstring

    payload_key = "movimentacoes"
    record_key = "COD_HISTORICO"
    error_key = "cod_historico"
    CHUNK = 200

    _BASE_SQL = """
        SELECT
            h.AHT_CODIGO,
            h.AHT_CODANIMAL      AS COD_ANIMAL,
            h.AHT_DATA           AS DATA_EVENTO,
            h.AHT_IDMOVIMENTACAO AS TIPO_ATMF,
            t.ATMF_STATUS        AS TIPO_STATUS,
            t.ATMF_TIPO          AS TIPO_DESCRICAO,
            h.AHT_CODLOTEORIGEM  AS LOTE_ORIGEM,
            h.AHT_CODLOTEDESTINO AS LOTE_DESTINO,
            h.AHT_CURRALORIGEM   AS CURRAL_ORIGEM,
            h.AHT_CURRALDESTINO  AS CURRAL_DESTINO,
            h.AHT_MOTIVO         AS MOTIVO
        FROM CAD_HISTORICOTRASNF h
        LEFT JOIN AUX_TIPO_MOV_FISICA t ON t.ATMF_ID = h.AHT_IDMOVIMENTACAO
    """
    _WHERE_INCREMENTAL = " WHERE h.AHT_CODIGO > ?"
    _ORDER_BY = " ORDER BY h.AHT_CODIGO"

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        if ctx.last_watermark is None:
            # first run (or after a reset): read the whole history once
            return ExtractSpec(sql=self._BASE_SQL + self._ORDER_BY, params=())
        sql = self._BASE_SQL + self._WHERE_INCREMENTAL + self._ORDER_BY
        return ExtractSpec(
            sql=sql,
            params=(watermark_param(ctx.last_watermark),),
            incremental=True,
        )

    def transform(self, row: dict) -> dict:
        record = {
            "COD_HISTORICO": req_str(row.get("AHT_CODIGO")),
            "COD_ANIMAL": req_str(row.get("COD_ANIMAL")),
            "DATA_EVENTO": iso_date(row.get("DATA_EVENTO")),
            "TIPO_ATMF": integer(row.get("TIPO_ATMF")),
            "TIPO_STATUS": opt_str(row.get("TIPO_STATUS")),      # ENTRADA | SAIDA
            "TIPO_DESCRICAO": opt_str(row.get("TIPO_DESCRICAO")),  # NASCIMENTO, PASTO, ...
            "LOTE_ORIGEM": opt_code(row.get("LOTE_ORIGEM")),
            "LOTE_DESTINO": opt_code(row.get("LOTE_DESTINO")),
            # curral columns hold the NAME (CC_NOME), not the code
            "CURRAL_ORIGEM": opt_str(row.get("CURRAL_ORIGEM")),
            "CURRAL_DESTINO": opt_str(row.get("CURRAL_DESTINO")),
            "MOTIVO": opt_str(row.get("MOTIVO")),
        }
        return record
