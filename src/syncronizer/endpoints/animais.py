"""TGC `animais` endpoint — sync the herd from Firebird (CAD_ANIMAL) to AgroDB.

Source: Firebird 2.5 `CAD_ANIMAL` (+ CAD_CATEGORIA, CAD_RACA).
Target: POST /api/integracoes/tgc/animais  body {"animais": [ ... ]} (1..500 itens).
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

The API upserts and returns 200 {inserted, updated, errors:[{cod_animal, message}]}, so
the batch send reconciles per-item failures by COD_ANIMAL (see ``_common.BatchEndpoint``).

Runs last of the reference feeds (order=30) so the curral and the lote an animal points
at have already been sent in the same cycle.

Contract v2 (jul/2026) — what changed against the first version:
  * DATA_REGISTRO (`CA_DATAREG`) is now sent: it is the immutable, trustworthy creation
    date. `CA_DATAENT` is user-editable and drifts, so it is no longer the only date.
  * RACA (via CAD_RACA) and NCF were added.
  * the exit block (SAIDA, DATA_SAIDA, PESO_SAIDA, VALOR_SAIDA) was added.
  * PESO_BALANCINHA was renamed PESO_ENTRADA and USUARIO was dropped.
  * CURRAL_ATUAL now carries the curral CODE (`CA_ULTIMO_CURRAL`), not the name. The
    destination stores it in `animais_tgc.cod_curral_atual` and joins it against
    `currais_tgc.cod_curral` (= CC_CODIGO) in `vw_ocupacao_currais`; sending the name
    made that join miss SILENTLY — the animal simply vanished from the occupancy screen
    with no error anywhere. The code is also the stable key: a farm renaming a curral
    would otherwise break the whole history. The name is not denormalized here because
    the `currais` feed already mirrors CAD_CURRAL, so AgroDB resolves it locally.
    NOTE the deliberate contrast with `movimentacoes`: AHT_CURRALORIGEM/DESTINO really
    are names in TGC and stay names (AgroDB has curral_origem_nome/curral_destino_nome
    for exactly that). Do not "unify" the two.

VALOR_ENTRADA (`CA_VALORENT`, ago/2026) — campo estrutural para o fallback de custo do
animal no AgroDB. Em Birigui ele vem hoje 0/NULL (verificado no backup real: 0% das
linhas preenchidas), mas o espelho precisa da coluna pronta para quando a fazenda
passar a preencher — sem ela o dado novo seria descartado em silêncio. O 0,00 é o
default intocado do TGC para "não informado" (mesmo racional do bloco de saída:
CA_VALORSAIDA fica 0,00 para todo o rebanho vivo), então 0 vira None no transform em
vez de virar um custo falso de R$ 0,00 justamente no campo que o AgroDB usaria como
fallback. Note o contraste deliberado com PESO_ENTRADA: peso 0 É enviado (problema de
qualidade que o destino deve enxergar e o TGC deve corrigir); valor 0,00 não é dado
ruim, é ausência de dado.

CAUSA_MORTE (`CA_CAUSAMORTE`, ago/2026, contrato v3) — morte é a única saída tratada
no escopo atual e precisa viajar COM a causa. Na base real ela só aparece preenchida
em animais com CA_SAIDA='MORTE' (39 animais, 16 causas distintas — de PNEUMONIA a
RAIO, incluindo lixo como "167-27", que viaja mesmo assim: sem quality gate local),
mas o envio NÃO é condicionado ao bloco de saída — dado preenchido viaja, seja qual
for a saída, para o espelho nunca descartar causa em silêncio se o TGC preencher
fora do padrão.

BLOCO DE ABATE (`CA_LOTESAIDA`, `CA_RC_SAIDA`, `CA_PESO_ARR_SAIDA`, ago/2026) — liga o
animal ao fechamento por lote de abate e traz o resultado individual do abate:

  * COD_LOTE_SAIDA (`CA_LOTESAIDA`) é a CHAVE DO FECHAMENTO: aponta para
    `lotes_saida_tgc.cod_lote_saida` (CLS_CODIGO, feed `lotes_saida`). Preenchida em
    3.184 dos 9.392 animais da base real — exatamente a mesma contagem das pesagens de
    tipo SAIDA — e nunca 0, com zero órfãos contra CAD_LOTESAIDA.
  * RC_SAIDA (`CA_RC_SAIDA`) e ARROBAS_SAIDA (`CA_PESO_ARR_SAIDA`) são o RC real e a
    arroba de carcaça POR ANIMAL. Preenchidas em 2.258 animais e NUNCA 0: ou o TGC tem
    o número, ou a coluna é NULL. Note a assimetria contra o COD_LOTE_SAIDA — 926
    animais estão num lote de abate sem RC/arroba individual, e o relatório precisa
    tratar isso como ausência, não como zero.

Os três NÃO entram no bloco condicional de saída (que existe porque CA_VALORSAIDA fica
0,00 no rebanho vivo inteiro): aqui não há sentinela zero a filtrar, e um dado que o
TGC preencher fora do padrão de SAIDA nunca deve ser descartado em silêncio — mesmo
racional da CAUSA_MORTE.

ATENÇÃO À ORDEM: este feed é order=30 e `lotes_saida` é order=61, então no primeiro
ciclo o animal chega ao destino antes do lote de abate que ele referencia. É
tolerável porque `animais_tgc.cod_lote_saida` é TEXT sem FK — a referência resolve no
mesmo ciclo, quando `lotes_saida` roda. Não inverta a ordem sem checar: `animais` é
pré-requisito de `pesagens` (order=60) e de todo o bloco de fechamento.

Renaming a field (PESO_BALANCINHA→PESO_ENTRADA) or adding a column (VALOR_ENTRADA,
CAUSA_MORTE, o bloco de abate) changes every ``row_hash``, so the first cycle after
such a change re-sends the whole herd once. That is intended — the destination is
rebuilding. VALOR_ENTRADA e CAUSA_MORTE entraram JUNTOS numa release; o bloco de abate
entra na seguinte, cada uma com o seu re-envio único do rebanho.

Data-quality problems (RC_ENTRADA > 100, duplicate SISBOV, ...) are NOT filtered out
locally — every animal is sent so the destination surfaces the issue. Corrections happen
in the source (TGC); the next sync detects the changed row (by COD_ANIMAL within the farm)
and re-sends it with the corrected value. The single exception is the chip literal
"9,63E+14" (see ``_common.clean_id``): that is not a low-quality identifier, it is a
destroyed one, and forwarding it would attach the same fake chip to 66 animals.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    clean_id,
    integer,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)


def _saida(value):
    """Real exit reason, or None for TGC's "still here" marker."""
    text = opt_str(value)
    if text is None or text.upper() == "NENHUM":
        return None
    return text


def _valor_entrada(value):
    """Custo de entrada, ou None para o 0,00 que o TGC deixa quando não informado.

    Mesmo racional do bloco de saída: 0,00 é o default intocado do TGC, não um custo
    real de R$ 0. Encaminhá-lo plantaria um custo falso exatamente no campo que o
    AgroDB usa como fallback (ver docstring do módulo).
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class AnimaisEndpoint(BatchEndpoint):
    name = "animais"                 # -> control table ep_animais
    primary_key = "COD_ANIMAL"       # unique within a farm (one syncronizer = one farm/token)
    order = 30
    api_path = "/api/integracoes/tgc/animais"
    api_method = "POST"

    # Full scan + row_hash change detection (only changed/new animals are re-sent).
    # To scope it (e.g. active head only) add a WHERE to _BASE_SQL.
    incremental_column = None
    reconcile_deletes = False        # exits are tracked via SAIDA, not row deletion

    payload_key = "animais"
    record_key = "COD_ANIMAL"
    error_key = "cod_animal"
    CHUNK = 200                      # animals per POST (<= API max 500, under rate limits)

    _BASE_SQL = """
        SELECT
            a.CA_CODIGO          AS COD_ANIMAL,
            a.CA_SISBOV          AS SISBOV,
            a.CA_CHIP            AS CHIP,
            a.CA_NCF             AS NCF,
            cat.CCAT_NOME        AS CATEGORIA,
            rac.CR_NOME          AS RACA,
            a.CA_CODLOTE_ENTRADA AS LOTE_ENTRADA,
            a.CA_CODLOTE         AS LOTE_ATUAL,
            a.CA_ULTIMO_CURRAL   AS CURRAL_ATUAL,
            a.CA_DATAREG         AS DATA_REGISTRO,
            a.CA_DATAENT         AS DATA_ENTRADA,
            a.CA_PESO_ENT        AS PESO_ENTRADA,
            a.CA_VALORENT        AS VALOR_ENTRADA,
            a.CA_RC_ENTRADA      AS RC_ENTRADA,
            a.CA_IDADE           AS IDADE,
            a.CA_NUMCONTRATO     AS NUM_CONTRATO,
            a.CA_SAIDA           AS SAIDA,
            a.CA_CAUSAMORTE      AS CAUSA_MORTE,
            a.CA_DATASAIDA       AS DATA_SAIDA,
            a.CA_PESO_SAIDA      AS PESO_SAIDA,
            a.CA_VALORSAIDA      AS VALOR_SAIDA,
            a.CA_LOTESAIDA       AS COD_LOTE_SAIDA,
            a.CA_RC_SAIDA        AS RC_SAIDA,
            a.CA_PESO_ARR_SAIDA  AS ARROBAS_SAIDA
        FROM CAD_ANIMAL a
        LEFT JOIN CAD_CATEGORIA cat ON cat.CCAT_CODIGO = a.CA_CODCATEGORIA
        LEFT JOIN CAD_RACA      rac ON rac.CR_CODIGO   = a.CA_CODRACA
        ORDER BY a.CA_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        lote_entrada = req_str(row.get("LOTE_ENTRADA"))
        record = {
            "COD_ANIMAL": req_str(row.get("COD_ANIMAL")),
            "CATEGORIA": opt_str(row.get("CATEGORIA")),
            "RACA": opt_str(row.get("RACA")),
            "LOTE_ENTRADA": lote_entrada,
            # live lote; if the source has no realocação, fall back to the entry lote
            "LOTE_ATUAL": req_str(row.get("LOTE_ATUAL")) or lote_entrada,
            # curral CODE (CC_CODIGO), the key currais_tgc.cod_curral is joined on —
            # NOT the name (see the contract note in the module docstring)
            "CURRAL_ATUAL": opt_code(row.get("CURRAL_ATUAL")),
            # immutable creation date — trust this one over DATA_ENTRADA
            "DATA_REGISTRO": iso_date(row.get("DATA_REGISTRO")),
            "DATA_ENTRADA": iso_date(row.get("DATA_ENTRADA")),
            "PESO_ENTRADA": num(row.get("PESO_ENTRADA")),
            # custo de entrada — 0,00 do TGC significa "não informado" e vira None
            # (ver _valor_entrada e a nota no docstring do módulo)
            "VALOR_ENTRADA": _valor_entrada(row.get("VALOR_ENTRADA")),
            "RC_ENTRADA": num(row.get("RC_ENTRADA")),
            "IDADE": integer(row.get("IDADE")),
            "NUM_CONTRATO": opt_str(row.get("NUM_CONTRATO")),
            # bloco de abate: chave do fechamento + resultado individual do abate.
            # Fora do gate do bloco de saída de propósito — não há sentinela zero aqui
            # (nunca 0 na base real, só NULL ou valor). Ver docstring do módulo.
            "COD_LOTE_SAIDA": opt_code(row.get("COD_LOTE_SAIDA")),
            "RC_SAIDA": num(row.get("RC_SAIDA")),
            "ARROBAS_SAIDA": num(row.get("ARROBAS_SAIDA")),
        }

        # identifiers: sent only when present (mirrors the validated payload)
        for key, val in (("SISBOV", clean_id(row.get("SISBOV"))),
                         ("CHIP", clean_id(row.get("CHIP"))),
                         ("NCF", opt_str(row.get("NCF")))):
            if val is not None:
                record[key] = val

        # causa da morte: enviada sempre que preenchida, fora do gate do bloco de
        # saída de propósito (ver docstring) — na base real só existe em SAIDA=MORTE
        causa_morte = opt_str(row.get("CAUSA_MORTE"))
        if causa_morte is not None:
            record["CAUSA_MORTE"] = causa_morte

        # exit block: only for an animal that actually left. TGC leaves CA_VALORSAIDA at
        # 0,00 for the whole live herd, so sending it unconditionally would ship a fake
        # "exit worth zero" for every animal still on the farm.
        saida = _saida(row.get("SAIDA"))
        if saida is not None:
            record["SAIDA"] = saida
            for key, val in (("DATA_SAIDA", iso_date(row.get("DATA_SAIDA"))),
                             ("PESO_SAIDA", num(row.get("PESO_SAIDA"))),
                             ("VALOR_SAIDA", num(row.get("VALOR_SAIDA")))):
                if val is not None:
                    record[key] = val
        return record
