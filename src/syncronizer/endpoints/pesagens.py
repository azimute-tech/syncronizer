"""TGC `pesagens` endpoint — pesagens de entrada e de abate (DET_PESAGEM) para o AgroDB.

Source: Firebird 2.5 `DET_PESAGEM` (12.576 linhas na base de staging).
Target: POST /api/integracoes/tgc/pesagens  body {"pesagens": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda depois de `animais` (order=60): o animal que a pesagem aponta já foi enviado no
mesmo ciclo (`DP_CODANIMAL` -> `animais_tgc.cod_animal`; zero órfãos conferidos na
staging).

Só existem dois tipos na origem: ENTRADA (9.392, uma por animal) e SAIDA (3.184, a
pesagem do abate — é ela que traz carcaça e RC REAIS). O relatório de fechamento lê o
tipo SAIDA; o tipo ENTRADA do TGC é REFERÊNCIA e nunca substitui o peso de entrada do
AgroDB, que sai da conferência/O.C. (regra de ouro do módulo).

FULL SCAN + row_hash — decisão deliberada CONTRA a linha "Incremental por DP_CODIGO
(append-only na prática)" do contrato, porque a base real desmente a premissa. Medido
na staging: 9.011 das 12.576 linhas (72%) têm `DP_DATA_UPDATE` POSTERIOR a
`DP_DATAREG`, em blocos espalhados por dezenas de dias — 1.145 em 09/07, 1.021 em
14/07 (estas TODAS do tipo SAIDA, ou seja, exatamente a carcaça/RC de que o
fechamento depende), 863 em 12/06, 792 em 11/06. Nenhuma dessas correções mudaria o
`DP_CODIGO`, então um watermark pela PK não veria nenhuma delas e o valor errado
ficaria congelado no espelho para sempre, EM SILÊNCIO. É o mesmo critério que já leva
`fornecimentos` e `leituras_cocho` a pagar o scan completo, e o custo aqui é da mesma
ordem (12,5k linhas contra as 13,0k de CAD_FORNECIMENTO). Voltar para incremental é
uma linha (``incremental_column = "DP_CODIGO"`` + WHERE), caso a decisão seja revista.

``reconcile_deletes`` fica False como nos demais feeds.

Zeros: peso vivo 0 não existe na base (0 linhas) e viaja como dado real se aparecer.
Já `DP_CARCACA` 0,0000 é o default "não informado" do TGC — 5 linhas, todas do tipo
SAIDA, com `DP_ARROBA` 0,0000 e `DP_RENDIMENTO` NULL nas MESMAS 5 linhas (conferido):
é abate sem carcaça informada, não carcaça de zero quilo. O contrato manda anular só
CARCACA_KG; ARROBAS recebe o mesmo tratamento aqui de propósito, porque arroba é
carcaça/15 na própria origem — deixar ARROBAS em 0.0 com CARCACA_KG em None faria o
destino fechar o abate com "0 @" reais em vez de "não informado".

DIVERGÊNCIAS medidas contra o contrato (documentadas, não corrigidas em silêncio):
  * `DP_CLASSIFICACAO` — o contrato diz "null na base atual"; na staging há 661 linhas
    com 'NORMAL'. O campo viaja normalmente.
  * `DP_DENTICAO` — o contrato o descreve como TEXT; na origem é INTEGER, e as 551
    linhas preenchidas trazem o valor 0 (as outras 12.025 são NULL). 0 é o sentinela
    "não informado" do TGC para código, então passa por ``opt_code`` (0 -> None) e
    chega ao destino como TEXT, respeitando o tipo do contrato.
  * `DP_SCORE` — o contrato o tipa NUMERIC(6,2); na origem é INTEGER com valores 1..3,
    preenchido só nas 9.392 pesagens de ENTRADA. Viaja como float via ``num``.
  * `DP_GORDURA` (acabamento) — 0 linhas preenchidas, como o contrato previa.

Sem quality gate local: `DP_RENDIMENTO` vai de 35,25 a 110,52 na base real (RC acima
de 100 é impossível), e essas linhas viajam assim mesmo para o destino enxergar o
problema. A correção acontece no TGC e o ``row_hash`` re-envia a linha corrigida.

Escalas (o driver firebirdsql já aplica): DP_PESO/DP_RENDIMENTO/DP_HORASJEJUM são
BIGINT scale -2 (450.00 kg, 53.25 %, horas); DP_CARCACA/DP_ARROBA são scale -4
(239.6422 kg, 15.9761 @).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)


def _carcaca(value):
    """Carcaça (ou arroba de carcaça), ou None para o 0,0000 de "não informado".

    Abate sem carcaça informada — 5 linhas na staging, todas com arroba 0 e RC NULL
    junto. Encaminhar 0 plantaria um fechamento de "0 kg / 0 @" reais (ver docstring).
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class PesagensEndpoint(BatchEndpoint):
    name = "pesagens"                # -> control table ep_pesagens
    primary_key = "COD_PESAGEM"      # PK real da tabela (DP_CODIGO)
    order = 60
    api_path = "/api/integracoes/tgc/pesagens"
    api_method = "POST"

    # Full scan + row_hash: 72% das linhas sofrem UPDATE depois de gravadas, inclusive
    # as de SAIDA que carregam carcaça e RC — ver a análise no docstring do módulo.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "pesagens"
    record_key = "COD_PESAGEM"
    error_key = "cod_pesagem"

    _BASE_SQL = """
        SELECT
            p.DP_CODIGO         AS COD_PESAGEM,
            p.DP_CODANIMAL      AS COD_ANIMAL,
            p.DP_TIPOPESAGEM    AS TIPO,
            p.DP_DATA           AS DATA,
            p.DP_PESO           AS PESO_KG,
            p.DP_CARCACA        AS CARCACA_KG,
            p.DP_ARROBA         AS ARROBAS,
            p.DP_RENDIMENTO     AS RENDIMENTO_PCT,
            p.DP_CLASSIFICACAO  AS CLASSIFICACAO,
            p.DP_DENTICAO       AS DENTICAO,
            p.DP_GORDURA        AS GORDURA,
            p.DP_SCORE          AS SCORE,
            p.DP_HORASJEJUM     AS HORAS_JEJUM
        FROM DET_PESAGEM p
        ORDER BY p.DP_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_PESAGEM": req_str(row.get("COD_PESAGEM")),
            "COD_ANIMAL": req_str(row.get("COD_ANIMAL")),
            "TIPO": req_str(row.get("TIPO")),               # ENTRADA | SAIDA
            "DATA": iso_date(row.get("DATA")),
            # peso vivo: 0 seria dado real (não ocorre na base) e viajaria como 0.0
            "PESO_KG": num(row.get("PESO_KG")),
            # carcaça/arroba 0,0000 = abate sem carcaça informada -> None (ver docstring)
            "CARCACA_KG": _carcaca(row.get("CARCACA_KG")),
            "ARROBAS": _carcaca(row.get("ARROBAS")),
            # RC; 50,00 fixo nas de ENTRADA e >100 em algumas de SAIDA — sem gate local
            "RENDIMENTO_PCT": num(row.get("RENDIMENTO_PCT")),
            "CLASSIFICACAO": opt_str(row.get("CLASSIFICACAO")),
            # DP_DENTICAO é INTEGER na origem e só traz 0 ("não informado") -> None
            "DENTICAO": opt_code(row.get("DENTICAO")),
            "GORDURA": opt_str(row.get("GORDURA")),         # acabamento
            # DP_SCORE é INTEGER 1..3 na origem; float para casar o tipo do contrato
            "SCORE": num(row.get("SCORE")),
            "HORAS_JEJUM": num(row.get("HORAS_JEJUM")),
        }
