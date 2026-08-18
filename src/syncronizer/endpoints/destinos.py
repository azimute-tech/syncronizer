"""TGC `destinos` endpoint — cadastro de destinos de abate (CAD_ESTGTA).

Source: Firebird 2.5 `CAD_ESTGTA` (9 linhas na base de staging) — o cadastro de
        estabelecimentos/GTA que `CAD_LOTESAIDA.CLS_CODDESTINO` referencia.
Target: POST /api/integracoes/tgc/destinos  body {"destinos": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

PARA QUE SERVE: dar NOME ao destino nos relatórios de fechamento/financeiro. O
`cod_destino` de `lotes_saida_tgc` aparecia como "6" na tela — o dono lê "MARFRIG
GLOBAL FOOD, Promissão". Pedido do Nelson em 17/08/2026; era backlog declarado desde
a onda 039.

Roda cedo (order=20), junto dos demais catálogos: `lotes_saida` (order maior)
referencia o código e o relatório resolve o nome via LEFT JOIN — órfão viaja como
código cru, nunca some.

FULL SCAN + row_hash — unidades de linhas, e o cadastro é editável no TGC (corrigir
o nome do frigorífico tem que refletir no relatório). Sem watermark útil; mesmo
critério de `racoes`, `currais` e `metas_abate`. ``reconcile_deletes`` False como nos
demais feeds.

O cadastro tem linhas-modelo do próprio TGC ("ALTERE P\\ NOME DA FAZENDA") — viajam
como qualquer outra: filtrá-las aqui seria opinião do pipeline sobre o dado; quem
nunca as referencia em lote de saída nunca as verá num relatório.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    opt_code,
    opt_str,
    req_str,
)


class DestinosEndpoint(BatchEndpoint):
    name = "destinos"            # -> control table ep_destinos
    primary_key = "COD_DESTINO"  # PK real da tabela (CGTA_CODIGO)
    order = 20
    api_path = "/api/integracoes/tgc/destinos"
    api_method = "POST"

    # Full scan + row_hash: unidades de linhas, cadastro editável.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "destinos"
    record_key = "COD_DESTINO"
    error_key = "cod_destino"

    _BASE_SQL = """
        SELECT
            g.CGTA_CODIGO   AS COD_DESTINO,
            g.CGTA_NOME     AS NOME,
            g.CGTA_CPF_CNPJ AS CPF_CNPJ,
            g.CGTA_ESTAB    AS ESTABELECIMENTO,
            g.CGTA_MUNIC    AS MUNICIPIO,
            g.CGTA_UF       AS UF
        FROM CAD_ESTGTA g
        ORDER BY g.CGTA_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_DESTINO": req_str(row.get("COD_DESTINO")),
            "NOME": opt_str(row.get("NOME")),
            "CPF_CNPJ": opt_code(row.get("CPF_CNPJ")),
            "ESTABELECIMENTO": opt_str(row.get("ESTABELECIMENTO")),
            "MUNICIPIO": opt_str(row.get("MUNICIPIO")),
            "UF": opt_str(row.get("UF")),
        }
