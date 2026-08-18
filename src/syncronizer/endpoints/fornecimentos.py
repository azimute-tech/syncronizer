"""TGC `fornecimentos` endpoint — tratos diários (CAD_FORNECIMENTO) para o AgroDB.

Source: Firebird 2.5 `CAD_FORNECIMENTO` (11,6k linhas na base real, ~64/dia).
Target: POST /api/integracoes/tgc/fornecimentos  body {"fornecimentos": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda depois dos feeds de referência (order=50): o lote e o curral que um trato aponta
já foram enviados no mesmo ciclo.

A DATA do trato é `CFORN_DATAFORNECIDO` (a data em que o trato foi fornecido), NÃO
`CFORN_DATAREG` (a data em que foi registrado). Na base real 114 linhas divergem —
trato do fim do dia registrado na manhã seguinte — e usar a data de registro jogaria
o consumo no dia errado. Não existe coluna `CFORN_DATA`.

FILTRO D-1 — `WHERE CFORN_DATAFORNECIDO < CURRENT_DATE`. O dia corrente fica de fora
de propósito: os tratos são lançados ao longo do dia (1..4 na base real) e a sobra de
cocho só fecha no dia seguinte; espelhar o dia aberto mandaria um consumo parcial que
o AgroDB leria como consumo baixo. O dia fechado entra completo no ciclo seguinte.

Full scan + row_hash: uma correção em trato antigo (o TGC mantém CFORN_DATA_UPDATE
justamente porque isso acontece) muda o hash e re-envia a linha — um watermark por
data perderia essas correções. ``reconcile_deletes`` fica False como nos demais
feeds; a armadilha conhecida é LOG_DELETA_TRATO (trato PODE ser deletado no TGC), e
enquanto a reconciliação de deleção não existir no destino, um trato deletado
persiste no espelho — documentado aqui para não virar surpresa.

Zeros: kg zerado É dado real (2.443 tratos com REALIZADO 0,0000 na base — trato
previsto e não fornecido) e viaja como 0.0. Já `CFORN_CUSTO` 0,0000 (1.838 linhas)
é o default "não informado" do TGC — vira None em vez de plantar custo alimentar
falso de R$ 0/kg (mesmo racional do VALOR_ENTRADA em `animais`).

Escalas: PREVISTO/REALIZADO/SOBRACOCHO/MSRACAO/CUSTO são BIGINT scale -4; o driver
firebirdsql devolve Decimal já com a escala aplicada (252.0691 kg, 56.8620 %MS,
0.5679 R$/kg — conferido na base real).

COD_RACAO_PROD (`CFORN_CODRACAOPROD`, ago/2026) — a dieta que o trato entregou, para o
corte por dieta no relatório de Tratos. Aponta para `racoes_tgc.cod_racao_prod`
(CRP_CODIGO, feed `racoes` order=20, que já roda antes deste). Na base real está
preenchida em 13.082/13.082 linhas, nunca 0 e nunca NULL, com zero órfãos contra
CAD_RACAO_PROD — ainda assim passa por ``opt_code`` para que o 0 sentinela do TGC, se
aparecer, não vire referência pendurada no destino. NÃO confundir com
`CFORN_CODRACAOPROD_PREV` (a dieta PREVISTA pelo plano) nem com
`CFORN_CODRACAOPROD_FORN`, que existem na mesma tabela: o relatório quer a dieta
efetivamente fornecida.

Acrescentar uma coluna muda todo ``row_hash``, então o primeiro ciclo depois desta
release re-envia os tratos uma vez. É intencional — o destino está preenchendo a
coluna nova.
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


def _custo(value):
    """Custo R$/kg MN, ou None para o 0,0000 que o TGC deixa quando não informado."""
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class FornecimentosEndpoint(BatchEndpoint):
    name = "fornecimentos"           # -> control table ep_fornecimentos
    primary_key = "COD_FORNECIMENTO"  # PK real da tabela (CFORN_CODIGO)
    order = 50
    api_path = "/api/integracoes/tgc/fornecimentos"
    api_method = "POST"

    # Full scan + row_hash: correções em tratos antigos precisam re-enviar (ver docstring).
    incremental_column = None
    reconcile_deletes = False        # LOG_DELETA_TRATO — ver a nota no docstring

    payload_key = "fornecimentos"
    record_key = "COD_FORNECIMENTO"
    error_key = "cod_fornecimento"

    _BASE_SQL = """
        SELECT
            f.CFORN_CODIGO         AS COD_FORNECIMENTO,
            f.CFORN_DATAFORNECIDO  AS DATA,
            f.CFORN_CODLOTE        AS COD_LOTE,
            f.CFORN_CODBAIA        AS COD_CURRAL,
            f.CFORN_CODRACAOPROD   AS COD_RACAO_PROD,
            f.CFORN_TRATO          AS TRATO,
            f.CFORN_PREVISTO       AS PREVISTO_KG,
            f.CFORN_REALIZADO      AS REALIZADO_KG,
            f.CFORN_SOBRACOCHO     AS SOBRA_KG,
            f.CFORN_QTDECAB        AS QTD_CABECAS,
            f.CFORN_MSRACAO        AS MS_PCT,
            f.CFORN_CUSTO          AS CUSTO_KG_MN
        FROM CAD_FORNECIMENTO f
        WHERE f.CFORN_DATAFORNECIDO < CURRENT_DATE
        ORDER BY f.CFORN_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_FORNECIMENTO": req_str(row.get("COD_FORNECIMENTO")),
            # data do FORNECIMENTO (CFORN_DATAFORNECIDO), nunca a de registro
            "DATA": iso_date(row.get("DATA")),
            "COD_LOTE": req_str(row.get("COD_LOTE")),
            # CFORN_CODBAIA é o CÓDIGO do curral (CC_CODIGO) — join limpo conferido na base
            "COD_CURRAL": opt_code(row.get("COD_CURRAL")),
            # dieta EFETIVAMENTE fornecida (CRP_CODIGO) — join com racoes_tgc;
            # não é a prevista (CFORN_CODRACAOPROD_PREV), ver docstring
            "COD_RACAO_PROD": opt_code(row.get("COD_RACAO_PROD")),
            "TRATO": integer(row.get("TRATO")),
            # kg 0 é dado real (trato previsto e não fornecido) — viaja como 0.0
            "PREVISTO_KG": num(row.get("PREVISTO_KG")),
            "REALIZADO_KG": num(row.get("REALIZADO_KG")),
            "SOBRA_KG": num(row.get("SOBRA_KG")),
            "QTD_CABECAS": integer(row.get("QTD_CABECAS")),
            "MS_PCT": num(row.get("MS_PCT")),
            # custo 0,0000 = "não informado" do TGC — vira None (ver docstring)
            "CUSTO_KG_MN": _custo(row.get("CUSTO_KG_MN")),
        }
