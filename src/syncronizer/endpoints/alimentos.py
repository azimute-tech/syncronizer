"""TGC `alimentos` endpoint — catálogo de ingredientes (CAD_ALIMENTO) para o AgroDB.

Source: Firebird 2.5 `CAD_ALIMENTO` (137 linhas na base de staging em 20/10/2026; eram
        113 em 08/2026).
Target: POST /api/integracoes/tgc/alimentos  body {"alimentos": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda cedo (order=20), com os demais catálogos: `batidas_itens` (order=56),
`insumos_entradas` (57) e `insumos_baixas` (58) apontam para `AA_CODIGO` e precisam do
nome já espelhado — zero órfãos nos três, conferido na staging.

PARA QUE SERVE: dar NOME e CLASSE ao ingrediente nos relatórios de consumo. `AA_TIPO`
já traz a classe pronta na origem (VOLUMOSO 24, MINERAL 26, COMERCIAL 25, ENERGETICO
23, PROTEICO 14, AGUA-PADRAO 1), o que evita inferir a classe do nome do ingrediente.

FULL SCAN + row_hash — ~140 linhas, e o catálogo é EDITADO por natureza:
`AA_CUSTOMEDIO` é recalculado a cada compra (a origem mantém `AA_DATA_UPDATE` por isso)
e o silo encerrado sai da posição virando `AA_REL_ESTOQUE_ATUAL = 'N'`. Um watermark por
`AA_CODIGO` congelaria nome, tipo, custo médio e status no estado do cadastro inicial.
Mesmo critério de `currais` e `metas_abate`. ``reconcile_deletes`` fica False como nos
demais feeds.

Zeros: `AA_CUSTOMEDIO` 0,0000 é o default "não informado" do TGC — 77 das 113 linhas
em 08/2026, ingredientes cadastrados e nunca comprados — e vira None em vez de plantar
um ingrediente que "custa R$ 0/kg", que é exatamente o erro que zeraria o custo
alimentar de uma dieta inteira no relatório. Mesmo racional do CUSTO_KG_MN em
`fornecimentos`.

ESTOQUE (Relatório de Insumos, contrato seção 12) — o que a Posição de Estoque do TGC
(`PROC_ESTOQUE_POSICAO`) lê do catálogo. Medido na staging (137 linhas):
  * `CONTROLA_ESTOQUE` = `AA_REL_ESTOQUE_ATUAL = 'S'` — o mesmo predicado da Posição
    do TGC (NULL ou 'N' são False). 'S' em 129, 'N' em 8 (silos/ingredientes
    encerrados: a posição atual do AgroDB os esconde, o movimento do período não).
  * `ESTOQUE_MINIMO_KG` (AA_ESTOQUEMINIMO): 0 vira None — e é 0 em 137/137, ou seja,
    ninguém cadastrou mínimo em kg nesta base. A coluna entra pronta.
  * `ESTOQUE_MINIMO_DIAS` (AA_ESTOQUE_MIN_DIAS, INTEGER): 0 vira None. 20 em 58, NULL
    em 79, nenhum 0.
  * `MS_PCT` (AA_MS): 0 vira None — só a AGUA-PADRAO (10000) tem 0,00; os minerais
    têm 100,00, que é dado real.
  * `UNIDADE` (AA_UN_MEDIDA): como veio — 'KG' em 137/137.
  * `DATA_ULT_CUSTO` (AA_DATAULTCUSTO): DIVERGÊNCIA medida — NULL em 137/137, apesar do
    nome; o TGC desta base não grava a data do recálculo do custo médio. A coluna
    entra pronta para quando gravar.

Acrescentar colunas muda todo ``row_hash``: o primeiro ciclo depois desta versão
re-envia o catálogo inteiro uma vez (~140 linhas). Ordem de deploy: AgroDB primeiro
(migration 074 + rota aceitando os campos novos) — senão a rota antiga descarta os
campos e a linha fica marcada como enviada até o próximo recálculo do custo.

Escalas: `AA_CUSTOMEDIO` é BIGINT scale -4; `AA_ESTOQUEMINIMO` e `AA_MS` são BIGINT
scale -2. O driver firebirdsql devolve Decimal com a escala já aplicada.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    flag_sn,
    integer,
    iso_date,
    num,
    opt_str,
    req_str,
)


def _custo_medio(value):
    """Custo médio em R$/kg, ou None para o 0,0000 de "não informado" do TGC.

    77 das 113 linhas da staging estão nesse default (ingrediente cadastrado e nunca
    comprado); encaminhar 0 zeraria o custo alimentar da dieta que o usa.
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


def _sem_zero(value):
    """Número, ou None para o 0 de "não cadastrado" do TGC (mínimo em kg, %MS)."""
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


def _dias_sem_zero(value):
    """Dias (INTEGER), ou None para o 0 de "sem mínimo cadastrado"."""
    dias = integer(value)
    if dias is None or dias == 0:
        return None
    return dias


class AlimentosEndpoint(BatchEndpoint):
    name = "alimentos"               # -> control table ep_alimentos
    primary_key = "COD_ALIMENTO"     # PK real da tabela (AA_CODIGO)
    order = 20
    api_path = "/api/integracoes/tgc/alimentos"
    api_method = "POST"

    # Full scan + row_hash: ~140 linhas, e o custo médio muda a cada compra.
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
            a.AA_CUSTOMEDIO AS CUSTO_MEDIO_RS,
            a.AA_REL_ESTOQUE_ATUAL AS CONTROLA_ESTOQUE,
            a.AA_ESTOQUEMINIMO     AS ESTOQUE_MINIMO_KG,
            a.AA_ESTOQUE_MIN_DIAS  AS ESTOQUE_MINIMO_DIAS,
            a.AA_MS                AS MS_PCT,
            a.AA_UN_MEDIDA         AS UNIDADE,
            a.AA_DATAULTCUSTO      AS DATA_ULT_CUSTO
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
            # --- estoque (contrato seção 12) ---
            # AA_REL_ESTOQUE_ATUAL = 'S': entra na Posição de Estoque; 'N' = encerrado
            "CONTROLA_ESTOQUE": flag_sn(row.get("CONTROLA_ESTOQUE")),
            # 0 = sem mínimo cadastrado -> None (0 em 137/137 na staging)
            "ESTOQUE_MINIMO_KG": _sem_zero(row.get("ESTOQUE_MINIMO_KG")),
            "ESTOQUE_MINIMO_DIAS": _dias_sem_zero(row.get("ESTOQUE_MINIMO_DIAS")),
            # 0,00 = não cadastrado (AGUA-PADRAO) -> None; 100,00 dos minerais é real
            "MS_PCT": _sem_zero(row.get("MS_PCT")),
            "UNIDADE": opt_str(row.get("UNIDADE")),
            # NULL em 137/137 na staging (ver docstring)
            "DATA_ULT_CUSTO": iso_date(row.get("DATA_ULT_CUSTO")),
        }
