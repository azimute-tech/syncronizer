"""TGC `controle_diario` endpoint — gabarito diário do lote (CONTROLE_DIARIO).

Source: Firebird 2.5 `CONTROLE_DIARIO` (6,8k linhas na base real, 2 por lote/dia).
Target: POST /api/integracoes/tgc/controle-diario  body {"controle_diario": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda por último do bloco de consumo (order=54).

É a série lote×dia que o próprio TGC calcula (consumo, GMD médio, peso projetado,
previsão de abate) — o AgroDB usa como gabarito de reconciliação dos cálculos
próprios e como série histórica pronta.

CHAVE — `CNT_CODIGO` é a PK física, mas a identidade de NEGÓCIO é
COD_LOTE|DATA|TIPO (única nas 6.847 linhas da base real, conferido): um re-cálculo
no TGC pode recriar a linha com outro CNT_CODIGO e o espelho deve sobrescrever o
mesmo dia, não duplicá-lo. O transform materializa essa identidade no campo CHAVE
(também é o que a API ecoa em errors[].chave); ``primary_key`` composto gera o
mesmo valor para a tabela de controle local.

TIPO (`CNT_TIPO`) — GERAL | REBANHO (3.409 / 3.438 na base): duas visões de cálculo
do mesmo lote×dia; as duas viajam e o destino escolhe qual ler.

CURRAL_NOME — `CNT_CURRAL` existe mas é VARCHAR(30) com o NOME do curral ("B-1",
"RMG-2"), não o código. Vai como CURRAL_NOME, seguindo o precedente de
`movimentacoes` (nome fica nome; AgroDB resolve localmente contra currais_tgc).
Chamá-lo de COD_CURRAL repetiria o bug silencioso de join documentado em `animais`.

DATA_ABATE_PREVISTA — `CNT_DATA_ABATE_DIAS` (a previsão pela curva de dias), que é
a OFICIAL da fazenda; a tabela tem outras duas (CNT_DATA_ABATE, CNT_DATA_ABATE_BND)
que NÃO viajam. CNT_EFBIO NÃO viaja: fora de escala, comprovado na pesquisa.

FILTRO D-1 — `WHERE CNT_DATA < CURRENT_DATE`, como nos feeds de fornecimento e
leitura: a linha do dia corrente é recalculada ao longo do dia.

Escalas: as métricas são BIGINT scale -6; o driver devolve Decimal já escalado
(CAB 63.000000, GMD 1.653788). Cabeças/mortes viram int; o resto segue float cru —
inclusive zeros, que aqui são resultado de cálculo, não "não informado".
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    integer,
    iso_date,
    num,
    opt_str,
    req_str,
)


class ControleDiarioEndpoint(BatchEndpoint):
    name = "controle_diario"         # -> control table ep_controle_diario
    # identidade de negócio: um re-cálculo troca o CNT_CODIGO mas não o lote/dia/tipo
    primary_key = ("COD_LOTE", "DATA", "TIPO")
    order = 54
    api_path = "/api/integracoes/tgc/controle-diario"  # URL com hifen (padrao de rota do AgroDB); payload_key segue com underscore
    api_method = "POST"

    # Full scan + row_hash: o TGC recalcula dias já fechados; watermark perderia isso.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "controle_diario"
    record_key = "CHAVE"
    error_key = "chave"

    _BASE_SQL = """
        SELECT
            c.CNT_LOTE             AS COD_LOTE,
            c.CNT_DATA             AS DATA,
            c.CNT_TIPO             AS TIPO,
            c.CNT_CURRAL           AS CURRAL_NOME,
            c.CNT_CAB              AS CABECAS,
            c.CNT_CAB_MORTE        AS MORTES,
            c.CNT_DIAS_CONF        AS DIAS_CONF_MEDIO,
            c.CNT_PESO_ENTRADA     AS PESO_ENTRADA_MEDIO,
            c.CNT_PESO_MEDIO_ATUAL AS PESO_PROJETADO,
            c.CNT_GMD_MEDIO        AS GMD_MEDIO,
            c.CNT_CONSUMO_MS       AS CONSUMO_MS,
            c.CNT_CONSUMO_MN       AS CONSUMO_MN,
            c.CNT_IMS_PV           AS IMS_PV,
            c.CNT_DATA_ABATE_DIAS  AS DATA_ABATE_PREVISTA,
            c.CNT_ARRB_PROJ        AS ARROBAS_PROJ,
            c.CNT_RC_PROJ          AS RC_PROJ
        FROM CONTROLE_DIARIO c
        WHERE c.CNT_DATA < CURRENT_DATE
        ORDER BY c.CNT_DATA, c.CNT_LOTE, c.CNT_TIPO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        cod_lote = req_str(row.get("COD_LOTE"))
        data = iso_date(row.get("DATA"))
        tipo = req_str(row.get("TIPO"))
        return {
            # identidade de negócio materializada — é o que a API ecoa em errors[]
            "CHAVE": f"{cod_lote}|{data}|{tipo}",
            "COD_LOTE": cod_lote,
            "DATA": data,
            "TIPO": tipo,                        # GERAL | REBANHO
            # CNT_CURRAL é o NOME do curral, não o código (ver docstring)
            "CURRAL_NOME": opt_str(row.get("CURRAL_NOME")),
            "CABECAS": integer(row.get("CABECAS")),
            "MORTES": integer(row.get("MORTES")),
            "DIAS_CONF_MEDIO": num(row.get("DIAS_CONF_MEDIO")),
            "PESO_ENTRADA_MEDIO": num(row.get("PESO_ENTRADA_MEDIO")),
            "PESO_PROJETADO": num(row.get("PESO_PROJETADO")),
            "GMD_MEDIO": num(row.get("GMD_MEDIO")),
            "CONSUMO_MS": num(row.get("CONSUMO_MS")),
            "CONSUMO_MN": num(row.get("CONSUMO_MN")),
            "IMS_PV": num(row.get("IMS_PV")),
            # CNT_DATA_ABATE_DIAS — a previsão OFICIAL (nunca as outras duas colunas)
            "DATA_ABATE_PREVISTA": iso_date(row.get("DATA_ABATE_PREVISTA")),
            "ARROBAS_PROJ": num(row.get("ARROBAS_PROJ")),
            "RC_PROJ": num(row.get("RC_PROJ")),
        }
