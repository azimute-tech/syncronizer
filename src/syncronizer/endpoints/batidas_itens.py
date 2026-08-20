"""TGC `batidas_itens` endpoint — ingredientes da batida (DET_BATIDA) para o AgroDB.

Source: Firebird 2.5 `DET_BATIDA` (8.484 linhas na base de staging, ~5 por batida).
Target: POST /api/integracoes/tgc/batidas-itens  body {"itens": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

O item é o INGREDIENTE pesado dentro da batida: previsto x realizado em kg (matéria
natural e matéria seca) e o custo unitário do alimento. É ele que permite ver a
aderência da dieta ingrediente a ingrediente. Roda logo depois do cabeçalho
(order=56): a batida (`DBT_CODBATIDA` -> `batidas_tgc.cod_batida`) e o alimento
(`DBT_CODALIMENTO` -> `alimentos_tgc.cod_alimento`, order=20) já foram enviados no
mesmo ciclo — zero órfãos nos dois joins, conferido na staging.

FULL SCAN + row_hash — segue o cabeçalho por coerência, e pelo mesmo motivo dele: um
item é reescrito quando a batida é repesada ou o custo do alimento é recalculado, e o
`DBT_CODIGO` não muda nisso. Varrer os itens com um watermark enquanto o cabeçalho faz
full scan também produziria o pior dos mundos — cabeçalho corrigido no espelho e itens
congelados. ``reconcile_deletes`` fica False como nos demais feeds.

Zeros: kg 0 É dado real — 101 linhas com `DBT_QTDE` 0,0000 são ingredientes previstos
e não colocados na mistura, informação legítima de aderência — e viajam como 0.0. Já
`DBT_CUSTO` 0,0000 (90 linhas) é o default "não informado" do TGC e vira None, mesmo
racional do CUSTO_KG_MN em `fornecimentos`.

CUIDADO com a semântica de DBT_CUSTO: é o custo UNITÁRIO do alimento em R$/kg (0,1907
para 426 kg de um ingrediente), NÃO o custo total da linha. O contrato o nomeia
`custo_rs`, o que sugere um total — a soma de DBT_CUSTO x DBT_QTDE é que reproduz o
CBT_CUSTO do cabeçalho (740,10 contra 735,16 na batida 2173, diferença de
arredondamento do TGC). O destino precisa multiplicar pela quantidade antes de somar.

DIVERGÊNCIA medida contra o contrato (documentada, não corrigida em silêncio):
`DBT_MS` é NULL em 8.484/8.484 — o contrato tipa MS_PCT como NUMERIC(6,2), mas esta
base nunca preenche o campo. A coluna entra pronta para quando a fazenda passar a
preencher; sem ela o dado novo seria descartado em silêncio. O %MS efetivo pode ser
derivado no destino por `REAL_KG_MS / REALIZADO_KG` (ambos preenchidos: `DBT_REAL_KGMSP`
só é NULL em 5 linhas).

Escalas: DBT_QTDEPREVISTA/DBT_QTDE/DBT_PREV_KGMSP/DBT_REAL_KGMSP/DBT_MS/DBT_CUSTO são
BIGINT scale -4 e o driver firebirdsql devolve Decimal com a escala já aplicada
(426.0000 kg, 18.6929 kg MS, 0.1907 R$/kg).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    iso_date,
    num,
    opt_code,
    req_str,
)


def _custo(value):
    """Custo unitário do alimento em R$/kg, ou None para o 0,0000 de "não informado"."""
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class BatidasItensEndpoint(BatchEndpoint):
    name = "batidas_itens"           # -> control table ep_batidas_itens
    primary_key = "COD_ITEM"         # PK real da tabela (DBT_CODIGO)
    order = 56
    api_path = "/api/integracoes/tgc/batidas-itens"  # URL com hifen (padrao de rota do AgroDB); payload_key e "itens"
    api_method = "POST"

    # Full scan + row_hash: item reescrito quando a batida é corrigida, e coerência
    # obrigatória com o cabeçalho `batidas` — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "itens"
    record_key = "COD_ITEM"
    error_key = "cod_item"

    _BASE_SQL = """
        SELECT
            i.DBT_CODIGO       AS COD_ITEM,
            i.DBT_CODBATIDA    AS COD_BATIDA,
            i.DBT_CODALIMENTO  AS COD_ALIMENTO,
            i.DBT_DATA         AS DATA,
            i.DBT_QTDEPREVISTA AS PREVISTO_KG,
            i.DBT_QTDE         AS REALIZADO_KG,
            i.DBT_PREV_KGMSP   AS PREV_KG_MS,
            i.DBT_REAL_KGMSP   AS REAL_KG_MS,
            i.DBT_MS           AS MS_PCT,
            i.DBT_CUSTO        AS CUSTO_RS
        FROM DET_BATIDA i
        ORDER BY i.DBT_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_ITEM": req_str(row.get("COD_ITEM")),
            "COD_BATIDA": req_str(row.get("COD_BATIDA")),
            # join com alimentos_tgc.cod_alimento (AA_CODIGO)
            "COD_ALIMENTO": opt_code(row.get("COD_ALIMENTO")),
            "DATA": iso_date(row.get("DATA")),
            # kg 0 é dado real (ingrediente previsto e não colocado) — viaja como 0.0
            "PREVISTO_KG": num(row.get("PREVISTO_KG")),
            "REALIZADO_KG": num(row.get("REALIZADO_KG")),
            "PREV_KG_MS": num(row.get("PREV_KG_MS")),
            "REAL_KG_MS": num(row.get("REAL_KG_MS")),
            "MS_PCT": num(row.get("MS_PCT")),
            # custo UNITÁRIO em R$/kg; 0,0000 = "não informado" -> None (ver docstring)
            "CUSTO_RS": _custo(row.get("CUSTO_RS")),
        }
