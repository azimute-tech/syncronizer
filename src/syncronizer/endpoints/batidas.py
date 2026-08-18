"""TGC `batidas` endpoint — cabeçalho da batida de ração (CAD_BATIDA) para o AgroDB.

Source: Firebird 2.5 `CAD_BATIDA` (1.691 linhas na base de staging, 28/01 a 13/08).
Target: POST /api/integracoes/tgc/batidas  body {"batidas": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

A batida é a MISTURA que sai do vagão: previsto x realizado em kg, custo e ração
produzida. É o lado da FÁBRICA do consumo (o lado do COCHO é `fornecimentos`). Roda
antes dos itens (order=55), depois do catálogo `racoes` (order=20) que dá nome à
`CBT_CODRACAOPROD` — zero batidas órfãs conferidas na staging.

FULL SCAN + row_hash — mesmo raciocínio de `fornecimentos`: a origem mantém
`CBT_DATA_UPDATE` (preenchida em 1.691/1.691) justamente porque a batida É corrigida
depois de gravada (repesagem, custo recalculado quando o custo do alimento muda), e um
watermark por `CBT_CODIGO` não veria correção nenhuma — o id não muda. São 1.691
linhas, uma fração do que `fornecimentos` já varre por ciclo. ``reconcile_deletes``
fica False como nos demais feeds.

SEM FILTRO D-1, ao contrário de `fornecimentos` e `leituras_cocho` — e o contraste é
deliberado. Lá o filtro existe porque a linha é um AGREGADO DO DIA que só fecha à
meia-noite (tratos 1..4 e sobra de cocho); aqui cada linha é UM EVENTO discreto de
mistura, completo em si mesmo assim que é gravado, então espelhar o dia corrente não
manda dado parcial. `CBT_FLAG_FIM`, que seria a trava natural de "batida terminada",
está em 'N' nas 1.691 linhas — nunca é virada para 'S' nesta base, então não serve
como filtro e não é usada.

Zeros: kg 0 É dado real (batida prevista e não executada) e viaja como 0.0 — na
staging `CBT_QTDEBATIDA` nunca é 0 e `CBT_QTDEPREVISTA` é 0 em 1 linha. Já `CBT_CUSTO`
0,0000 é o default "não informado" do TGC (1 linha, além de 5 NULL) e vira None em vez
de plantar custo de ração falso de R$ 0 — mesmo racional do CUSTO_KG_MN em
`fornecimentos`.

CBT_CUSTO é o custo TOTAL da batida em R$ (735,16 para 1.088 kg), não R$/kg. Confira
com `batidas_itens`, onde DBT_CUSTO é o custo UNITÁRIO do ingrediente: a soma de
DBT_CUSTO x DBT_QTDE reproduz o CBT_CUSTO (740,10 x 735,16 na batida 2173 — a
diferença é arredondamento do TGC).

DIVERGÊNCIAS medidas contra o contrato (documentadas, não corrigidas em silêncio):
  * `CBT_QTDECABECA` é NULL em 1.691/1.691 — o contrato tipa QTD_CABECAS como INTEGER,
    mas esta base nunca preenche o campo. A coluna entra pronta para quando a fazenda
    passar a preencher; sem ela o dado novo seria descartado em silêncio.
  * `CBT_INICIO` / `CBT_FIM` são 00:00:00 em 1.691/1.691 — o default intocado do TIME.
    O contrato pede a hora como texto HH:MM sem regra de ausência, então "00:00" viaja
    como está (sem quality gate local, como em todo o resto do agente) e o DESTINO é
    quem deve tratar 00:00 como "não informado" ao montar o relatório. Se a regra
    tiver que virar None, ela pertence ao contrato, não a este transform.
  * `CBT_NOME_MOTORISTA` é NULL em 1.691/1.691 e `CBT_TIPO` é 'CONSUMO' em 1.691/1.691
    (não há variedade de tipo nesta base).

Escalas: CBT_QTDEPREVISTA/CBT_QTDEBATIDA/CBT_CUSTO são BIGINT scale -4 e o driver
firebirdsql devolve Decimal com a escala já aplicada (1099.9700 kg, 735.1644 R$).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    hhmm,
    integer,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)


def _custo(value):
    """Custo total da batida em R$, ou None para o 0,0000 de "não informado" do TGC."""
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class BatidasEndpoint(BatchEndpoint):
    name = "batidas"                 # -> control table ep_batidas
    primary_key = "COD_BATIDA"       # PK real da tabela (CBT_CODIGO)
    order = 55
    api_path = "/api/integracoes/tgc/batidas"
    api_method = "POST"

    # Full scan + row_hash: CAD_BATIDA mantém CBT_DATA_UPDATE porque a batida é
    # corrigida depois de gravada — ver docstring (mesmo racional de `fornecimentos`).
    incremental_column = None
    reconcile_deletes = False

    payload_key = "batidas"
    record_key = "COD_BATIDA"
    error_key = "cod_batida"

    _BASE_SQL = """
        SELECT
            b.CBT_CODIGO         AS COD_BATIDA,
            b.CBT_CODRACAOPROD   AS COD_RACAO_PROD,
            b.CBT_DATA           AS DATA,
            b.CBT_QTDEPREVISTA   AS QTDE_PREVISTA_KG,
            b.CBT_QTDEBATIDA     AS QTDE_REALIZADA_KG,
            b.CBT_QTDECABECA     AS QTD_CABECAS,
            b.CBT_CUSTO          AS CUSTO_RS,
            b.CBT_INICIO         AS HORA_INICIO,
            b.CBT_FIM            AS HORA_FIM,
            b.CBT_NOMEOPERADOR   AS OPERADOR,
            b.CBT_NOME_MOTORISTA AS MOTORISTA,
            b.CBT_TIPO           AS TIPO
        FROM CAD_BATIDA b
        ORDER BY b.CBT_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_BATIDA": req_str(row.get("COD_BATIDA")),
            # join com racoes_tgc.cod_racao_prod (CRP_CODIGO)
            "COD_RACAO_PROD": opt_code(row.get("COD_RACAO_PROD")),
            "DATA": iso_date(row.get("DATA")),
            # kg 0 é dado real (batida prevista e não executada) — viaja como 0.0
            "QTDE_PREVISTA_KG": num(row.get("QTDE_PREVISTA_KG")),
            "QTDE_REALIZADA_KG": num(row.get("QTDE_REALIZADA_KG")),
            "QTD_CABECAS": integer(row.get("QTD_CABECAS")),
            # custo 0,0000 = "não informado" do TGC — vira None (ver docstring)
            "CUSTO_RS": _custo(row.get("CUSTO_RS")),
            # TIME -> "HH:MM"; 00:00 em 100% da staging, viaja como está (ver docstring)
            "HORA_INICIO": hhmm(row.get("HORA_INICIO")),
            "HORA_FIM": hhmm(row.get("HORA_FIM")),
            "OPERADOR": opt_str(row.get("OPERADOR")),
            "MOTORISTA": opt_str(row.get("MOTORISTA")),
            "TIPO": opt_str(row.get("TIPO")),
        }
