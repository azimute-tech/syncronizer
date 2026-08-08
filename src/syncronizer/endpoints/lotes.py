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

Contrato v3 (ago/2026) — bloco de consumo/curvas:
  * COD_CURVA (`CLL_COD_CURVA`) — a curva de crescimento (DET_CATEGORIA) que o feed
    `curvas` espelha. 25 lotes na base real carregam 0 (= sem curva atribuída), o
    sentinelo padrão do TGC, então 0 vira None via ``opt_code`` como toda FK daqui.
  * DESTINO (`CLL_DESTINO`) — o nome da meta de abate (AUX_DESTINOANIMAL, feed
    `metas_abate`), ex. "MÉDIO 520". É NOME, não código: a PK real da tabela de
    metas é ADA_NOME.
  * CUSTO_FIXO_DIA (`CLL_CUSTOFIXO`) — diária fixa R$/cab/dia. BIGINT scale -4 no
    Firebird; o driver devolve Decimal já com a escala aplicada (2.5000 → 2.5).
    Base real: 130 lotes a 2,50 e 2 a 0,50 — sem zeros, valor baixo é valor real,
    então segue cru via ``num`` (sem regra 0→None).

Adicionar coluna muda o ``row_hash`` de TODOS os lotes: o primeiro ciclo depois
deste deploy re-envia o espelho de lotes inteiro uma única vez. É intencional —
o destino está sendo reconstruído com as colunas novas.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    integer,
    iso_date,
    num,
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
            l.CLL_NUMCONTRATO     AS NUM_CONTRATO,
            l.CLL_COD_CURVA       AS COD_CURVA,
            l.CLL_DESTINO         AS DESTINO,
            l.CLL_CUSTOFIXO       AS CUSTO_FIXO_DIA
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
            # curva de crescimento — 0 é o sentinelo "sem curva" do TGC (25 lotes na base)
            "COD_CURVA": opt_code(row.get("COD_CURVA")),
            # NOME da meta de abate (AUX_DESTINOANIMAL.ADA_NOME), ex. "MÉDIO 520"
            "DESTINO": opt_str(row.get("DESTINO")),
            # diária fixa R$/cab/dia, escala já aplicada pelo driver; 0,50 é valor real
            "CUSTO_FIXO_DIA": num(row.get("CUSTO_FIXO_DIA")),
        }
