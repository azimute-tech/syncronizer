"""TGC `leituras_cocho` endpoint — leitura de cocho (CAD_LEITURA) para o AgroDB.

Source: Firebird 2.5 `CAD_LEITURA` (3,4k linhas na base real).
Target: POST /api/integracoes/tgc/leituras-cocho  body {"leituras_cocho": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda junto do bloco de consumo (order=52), depois de currais/lotes.

FILTRO D-1 — `WHERE CLC_DATALEITURA < CURRENT_DATE`, mesmo racional do feed
`fornecimentos`: a leitura dirige o ajuste do trato do próprio dia, então o dia
corrente está em movimento; só o dia fechado espelha.

NOTA — CUIDADO: a nota de cocho NÃO é inteira. `CLC_NOTA` é BIGINT scale -2 e a
base real tem 288 leituras com nota 0,50 e 416 com 1,50 (escala −3,00..+3,00 em
meios pontos). Coagir para int silenciosamente arredondaria ~700 leituras
(0,5→0, 1,5→1), então a nota viaja como float via ``num``. NULL (628 linhas —
leitura lançada sem nota) segue None.

`CLC_KGMSCAB` / `CLC_KGMSCABAJUSTADO` / `CLC_KGMSREALIZADO` são DOUBLE (kg de MS
por cabeça: previsto pela dieta, ajustado pela nota, realizado). `CLC_AJUSTEKG` é
BIGINT scale -3; o driver devolve Decimal com a escala aplicada (-0.080). Ajuste 0
é dado real ("manter") e viaja como 0.0.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    integer,
    iso_date,
    num,
    opt_code,
    req_str,
)


class LeiturasCochoEndpoint(BatchEndpoint):
    name = "leituras_cocho"          # -> control table ep_leituras_cocho
    primary_key = "COD_LEITURA"      # PK real da tabela (CLC_CODIGO)
    order = 52
    api_path = "/api/integracoes/tgc/leituras-cocho"  # URL com hifen (padrao de rota do AgroDB); payload_key segue com underscore
    api_method = "POST"

    # Full scan + row_hash: ajuste lançado depois (CLC_DATAAJUSTE) altera a linha
    # e precisa re-enviar; um watermark por data perderia a correção.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "leituras_cocho"
    record_key = "COD_LEITURA"
    error_key = "cod_leitura"

    _BASE_SQL = """
        SELECT
            l.CLC_CODIGO          AS COD_LEITURA,
            l.CLC_DATALEITURA     AS DATA,
            l.CLC_CODBAIA         AS COD_CURRAL,
            l.CLC_CODLOTE         AS COD_LOTE,
            l.CLC_NOTA            AS NOTA,
            l.CLC_AJUSTEKG        AS AJUSTE_KG_MS,
            l.CLC_KGMSCAB         AS KGMS_CAB_PREVISTO,
            l.CLC_KGMSCABAJUSTADO AS KGMS_CAB_AJUSTADO,
            l.CLC_KGMSREALIZADO   AS KGMS_CAB_REALIZADO,
            l.CLC_QTDECABLOTE     AS QTD_CABECAS
        FROM CAD_LEITURA l
        WHERE l.CLC_DATALEITURA < CURRENT_DATE
        ORDER BY l.CLC_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_LEITURA": req_str(row.get("COD_LEITURA")),
            "DATA": iso_date(row.get("DATA")),
            # CLC_CODBAIA é o CÓDIGO do curral (CC_CODIGO) — join limpo conferido na base
            "COD_CURRAL": opt_code(row.get("COD_CURRAL")),
            "COD_LOTE": req_str(row.get("COD_LOTE")),
            # float de propósito: a base real tem notas 0,5 e 1,5 (ver docstring)
            "NOTA": num(row.get("NOTA")),
            "AJUSTE_KG_MS": num(row.get("AJUSTE_KG_MS")),
            "KGMS_CAB_PREVISTO": num(row.get("KGMS_CAB_PREVISTO")),
            "KGMS_CAB_AJUSTADO": num(row.get("KGMS_CAB_AJUSTADO")),
            "KGMS_CAB_REALIZADO": num(row.get("KGMS_CAB_REALIZADO")),
            "QTD_CABECAS": integer(row.get("QTD_CABECAS")),
        }
