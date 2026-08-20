"""TGC `racoes` endpoint — catálogo de rações produzidas (CAD_RACAO_PROD + CAD_RACAO).

Source: Firebird 2.5 `CAD_RACAO_PROD` (47 linhas na base de staging) com join em
        `CAD_RACAO` (47 linhas) só para trazer o NOME.
Target: POST /api/integracoes/tgc/racoes  body {"racoes": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda cedo (order=20), com os demais catálogos: `fornecimentos` (order=50) e `batidas`
(order=55) apontam para `CRP_CODIGO` e precisam do nome já espelhado — zero órfãos nos
dois joins, conferido na staging.

PARA QUE SERVE: dar NOME à dieta nos relatórios. O corte "adaptação / crescimento /
terminação" do BI antigo era INFERIDO do nome da ração; aqui ele vem do catálogo. Os
nomes reais da base confirmam o padrão ("ADAPTAÇÃO", "TERMINAÇÃO", "DIETA TIP_27/01",
"ADAP_31-01") — nome livre digitado pelo nutricionista, com e sem acento, o que é
exatamente o motivo de o corte ter que ser feito no destino sobre um NOME espelhado e
não numa heurística local.

DUAS TABELAS, DOIS CONCEITOS. `CAD_RACAO` é a FÓRMULA (a receita nutricional, com
CR_NOME, CR_MS, CR_PB...); `CAD_RACAO_PROD` é a VERSÃO PRODUZIDA daquela fórmula, e é
ela que batida e fornecimento referenciam. O espelho é a versão produzida, com o nome
da fórmula denormalizado — a fórmula inteira não interessa a nenhum relatório do
escopo, e espelhá-la seria um feed que ninguém lê. O join é LEFT de propósito: uma
versão apontando para uma fórmula inexistente deve viajar com NOME nulo para o destino
enxergar a falha, em vez de a linha sumir (na staging não há nenhuma, mas a garantia é
estrutural).

FULL SCAN + row_hash — 47 linhas, e o nome da fórmula é editável (renomear a dieta
tem que refletir no relatório). Não há coluna de watermark útil e um watermark por
`CRP_CODIGO` congelaria o nome. Mesmo critério de `currais` e `metas_abate`.
``reconcile_deletes`` fica False como nos demais feeds: uma ração sai de circulação
virando `CRP_STATUS = 'I'`, não sendo deletada — e a desativação viaja no payload.

`CRP_STATUS` é CHAR(1) 'A'/'I' (ativo/inativo — 3 'A' e 44 'I' na staging), NÃO o
'S'/'N' de flag booleana dos outros feeds, então viaja como texto via ``opt_str`` em
vez de virar boolean. Confundir os dois transformaria toda ração ativa em False.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    opt_code,
    opt_str,
    req_str,
)


class RacoesEndpoint(BatchEndpoint):
    name = "racoes"                  # -> control table ep_racoes
    primary_key = "COD_RACAO_PROD"   # PK real da tabela (CRP_CODIGO)
    order = 20
    api_path = "/api/integracoes/tgc/racoes"
    api_method = "POST"

    # Full scan + row_hash: 47 linhas, e renomear a dieta precisa re-enviar.
    incremental_column = None
    reconcile_deletes = False        # desativação viaja como STATUS='I' — ver docstring

    payload_key = "racoes"
    record_key = "COD_RACAO_PROD"
    error_key = "cod_racao_prod"

    _BASE_SQL = """
        SELECT
            rp.CRP_CODIGO   AS COD_RACAO_PROD,
            rp.CRP_CODRACAO AS COD_RACAO,
            r.CR_NOME       AS NOME,
            rp.CRP_STATUS   AS STATUS
        FROM CAD_RACAO_PROD rp
        LEFT JOIN CAD_RACAO r ON r.CR_CODIGO = rp.CRP_CODRACAO
        ORDER BY rp.CRP_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_RACAO_PROD": req_str(row.get("COD_RACAO_PROD")),
            # código da FÓRMULA (CAD_RACAO.CR_CODIGO), não da versão produzida
            "COD_RACAO": opt_code(row.get("COD_RACAO")),
            # nome da fórmula, denormalizado — é o que dá o corte da dieta no relatório
            "NOME": opt_str(row.get("NOME")),
            # 'A'/'I' na origem, NÃO 'S'/'N' — texto, nunca boolean (ver docstring)
            "STATUS": opt_str(row.get("STATUS")),
        }
