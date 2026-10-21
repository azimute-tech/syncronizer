"""TGC `alimentos` endpoint — catálogo de ingredientes (CAD_ALIMENTO) para o AgroDB.

Source: Firebird 2.5 `CAD_ALIMENTO` (113 linhas na base de staging).
Target: POST /api/integracoes/tgc/alimentos  body {"alimentos": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda cedo (order=20), com os demais catálogos: `batidas_itens` (order=56) aponta para
`AA_CODIGO` e precisa do nome já espelhado — zero itens órfãos, conferido na staging.

PARA QUE SERVE: dar NOME e CLASSE ao ingrediente nos relatórios de consumo. `AA_TIPO`
já traz a classe pronta na origem (VOLUMOSO 24, MINERAL 26, COMERCIAL 25, ENERGETICO
23, PROTEICO 14, AGUA-PADRAO 1), o que evita inferir a classe do nome do ingrediente.

FULL SCAN + row_hash — 113 linhas, e o catálogo é EDITADO por natureza: `AA_CUSTOMEDIO`
é recalculado a cada compra (a origem até mantém `AA_DATAULTCUSTO` e `AA_DATA_UPDATE`
por isso). Um watermark por `AA_CODIGO` congelaria nome, tipo e custo médio no estado
do cadastro inicial. Mesmo critério de `currais` e `metas_abate`.
``reconcile_deletes`` fica False como nos demais feeds.

Zeros: `AA_CUSTOMEDIO` 0,0000 é o default "não informado" do TGC — 77 das 113 linhas,
ingredientes cadastrados e nunca comprados — e vira None em vez de plantar um
ingrediente que "custa R$ 0/kg", que é exatamente o erro que zeraria o custo alimentar
de uma dieta inteira no relatório. Mesmo racional do CUSTO_KG_MN em `fornecimentos`.

Escala: `AA_CUSTOMEDIO` é BIGINT scale -4 e o driver firebirdsql devolve Decimal com a
escala já aplicada.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import BatchEndpoint, num, opt_str, req_str


def _custo_medio(value):
    """Custo médio em R$/kg, ou None para o 0,0000 de "não informado" do TGC.

    77 das 113 linhas da staging estão nesse default (ingrediente cadastrado e nunca
    comprado); encaminhar 0 zeraria o custo alimentar da dieta que o usa.
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class AlimentosEndpoint(BatchEndpoint):
    name = "alimentos"               # -> control table ep_alimentos
    primary_key = "COD_ALIMENTO"     # PK real da tabela (AA_CODIGO)
    order = 20
    api_path = "/api/integracoes/tgc/alimentos"
    api_method = "POST"

    # Full scan + row_hash: 113 linhas, e o custo médio muda a cada compra.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "alimentos"
    record_key = "COD_ALIMENTO"
    error_key = "cod_alimento"

    _BASE_SQL = """
        SELECT
            a.AA_CODIGO     AS COD_ALIMENTO,
            a.AA_NOME       AS NOME,
            a.AA_TIPO       AS TIPO,
            a.AA_CUSTOMEDIO AS CUSTO_MEDIO_RS
        FROM CAD_ALIMENTO a
        ORDER BY a.AA_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_ALIMENTO": req_str(row.get("COD_ALIMENTO")),
            "NOME": req_str(row.get("NOME")),
            # classe do ingrediente: VOLUMOSO | MINERAL | COMERCIAL | ENERGETICO | ...
            "TIPO": opt_str(row.get("TIPO")),
            # custo 0,0000 = "não informado" do TGC — vira None (ver _custo_medio)
            "CUSTO_MEDIO_RS": _custo_medio(row.get("CUSTO_MEDIO_RS")),
        }
