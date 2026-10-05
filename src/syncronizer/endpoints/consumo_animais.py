"""TGC `consumo_animais` endpoint — consumo e custo alimentar por ANIMAL x LOTE.

Source: Firebird 2.5 `DET_AC_RESUMO_2011` (ativos, 467 mil linhas na base de staging)
        + `DET_AC_RESUMO_INATIVOS` (vendidos, 419 mil), AGREGADAS aqui por animal x lote
        (12,7 mil linhas, 9.136 animais; ~5 s no backup de 09/09/2026).
Target: POST /api/integracoes/tgc/consumo-animais  body {"consumo_animais": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

POR QUE EXISTE — as duas tabelas são o consumo de cada animal em cada dia (uma linha
por animal x dia x lote x ração) e é a soma delas que todo fechamento do TGC usa
(`PROC_RESULTADO_INDIVIDUAL`, `PROC_ANALISE_LT_SAIDA`). O AgroDB refazia a conta a
partir do trato do lote, em partes iguais; o TGC rateia pelo PESO PROJETADO do animal
e registra o lote do dia por fotografia. As duas contas batem na média e divergem ~3%
por animal. Decisão do Nelson (01/10/2026): consumo individual é dado operacional do
TGC e passa a vir dele. Estudo: AgroDB `docs/relatorios/estudo-tgc/`.

GRÃO animal x lote, não animal x dia: o grão diário tem 886 mil linhas e cresce ~6,7
mil por dia (decisão D2 do espelho v3 segue valendo). O lote fica no grão porque trocar
de lote é manejo normal — 28% dos animais têm consumo em mais de um lote, há animal com
7 — e o consumo do animal é a soma dos lotes por onde passou.

DUAS JANELAS, somadas aqui porque só o Firebird tem o dia:
  total            tudo o que o animal comeu naquele lote;
  DESDE_ENTRADA    só os dias a partir de `CA_DATAENT` (entrada no confinamento).
Quem veio de recria/TIP tem consumo ANTES de `CA_DATAENT`; o TGC soma esse consumo e
divide pelos dias contados a partir da entrada. O AgroDB recebe as duas e escolhe.

FILTROS — os do próprio TGC, com a contagem do que ficou de fora viajando junto:
  - linha só soma com MS > 0, MN > 0 e custo/cab > 0 (o filtro de
    `PROC_RESULTADO_INDIVIDUAL`); as demais contam em LINHAS_DESCARTADAS;
  - linha com data posterior à saída do animal fica fora e conta em LINHAS_POS_SAIDA:
    o TGC continua gerando consumo para animal morto por até 29 dias (29 dos 43 mortos
    da staging), e só VENDA migra para a tabela de inativos;
  - D-1 (`DACRES_DATA < CURRENT_DATE`), como nos feeds de fornecimento e controle
    diário: o dia corrente é recontabilizado ao longo do dia.

FULL SCAN + row_hash — o TGC recontabiliza dias fechados (troca de custo de ração,
correção de trato) e a linha agregada muda sem deixar watermark. Só as linhas que
mudaram viajam: todo dia, as dos animais ativos (~5,8 mil); as de quem já saiu ficam
paradas. ``reconcile_deletes`` fica False como nos demais feeds — um animal x lote que
some da origem (reprocessamento) fica com a última soma conhecida no destino.

Somas arredondadas a 4 casas: são DOUBLE no Firebird e o ruído da 15ª casa mudaria o
row_hash a cada ciclo sem nada ter mudado.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    integer,
    iso_date,
    num,
    req_str,
)


def _soma(value):
    """Soma em kg ou R$, 4 casas; None quando o animal x lote não tem linha válida."""
    valor = num(value)
    if valor is None:
        return None
    return round(valor, 4)


class ConsumoAnimaisEndpoint(BatchEndpoint):
    name = "consumo_animais"         # -> control table ep_consumo_animais
    primary_key = ("COD_ANIMAL", "COD_LOTE")
    order = 64                       # depois de animais/lotes e do bloco de abate
    api_path = "/api/integracoes/tgc/consumo-animais"  # URL com hifen (padrao de rota do AgroDB)
    api_method = "POST"

    # Full scan + row_hash: recontabilização muda a soma sem watermark — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "consumo_animais"
    record_key = "CHAVE"
    error_key = "chave"

    _BASE_SQL = """
        SELECT
            x.COD_ANIMAL, x.COD_LOTE,
            MIN(CASE WHEN x.VALIDA = 1 THEN x.DATA END) AS DATA_INICIAL,
            MAX(CASE WHEN x.VALIDA = 1 THEN x.DATA END) AS DATA_FINAL,
            COUNT(DISTINCT CASE WHEN x.VALIDA = 1 THEN x.DATA END) AS DIAS,
            SUM(CASE WHEN x.VALIDA = 1 THEN x.MS END) AS CONSUMO_MS_KG,
            SUM(CASE WHEN x.VALIDA = 1 THEN x.MN END) AS CONSUMO_MN_KG,
            SUM(CASE WHEN x.VALIDA = 1 THEN x.CUSTO END) AS CUSTO_ALIM,
            COUNT(DISTINCT CASE WHEN x.VALIDA = 1 AND x.DESDE = 1 THEN x.DATA END) AS DIAS_DESDE_ENTRADA,
            SUM(CASE WHEN x.VALIDA = 1 AND x.DESDE = 1 THEN x.MS END) AS CONSUMO_MS_DESDE_ENTRADA_KG,
            SUM(CASE WHEN x.VALIDA = 1 AND x.DESDE = 1 THEN x.MN END) AS CONSUMO_MN_DESDE_ENTRADA_KG,
            SUM(CASE WHEN x.VALIDA = 1 AND x.DESDE = 1 THEN x.CUSTO END) AS CUSTO_ALIM_DESDE_ENTRADA,
            SUM(CASE WHEN x.POS_SAIDA = 0 THEN 1 ELSE 0 END) AS LINHAS,
            SUM(CASE WHEN x.POS_SAIDA = 0 AND x.VALIDA = 0 THEN 1 ELSE 0 END) AS LINHAS_DESCARTADAS,
            SUM(x.POS_SAIDA) AS LINHAS_POS_SAIDA
        FROM (
            SELECT
                r.COD_ANIMAL, r.COD_LOTE, r.DATA, r.MS, r.MN, r.CUSTO,
                CASE WHEN a.CA_SAIDA <> 'NENHUM' AND a.CA_DATASAIDA IS NOT NULL
                          AND r.DATA > a.CA_DATASAIDA THEN 1 ELSE 0 END AS POS_SAIDA,
                CASE WHEN r.MS > 0 AND r.MN > 0 AND r.CUSTO > 0
                          AND NOT (a.CA_SAIDA <> 'NENHUM' AND a.CA_DATASAIDA IS NOT NULL
                                   AND r.DATA > a.CA_DATASAIDA) THEN 1 ELSE 0 END AS VALIDA,
                CASE WHEN a.CA_DATAENT IS NULL OR r.DATA >= a.CA_DATAENT THEN 1 ELSE 0 END AS DESDE
            FROM (
                SELECT DACRES_CODANIMAL AS COD_ANIMAL, DACRES_CODLOTE AS COD_LOTE, DACRES_DATA AS DATA,
                       DACRES_CONSUMO_MS AS MS, DACRES_CONSUMO_MN AS MN, DACRES_CUSTOALIM_CAB AS CUSTO
                FROM DET_AC_RESUMO_2011
                WHERE DACRES_DATA < CURRENT_DATE
                UNION ALL
                SELECT DACRES_CODANIMAL, DACRES_CODLOTE, DACRES_DATA,
                       DACRES_CONSUMO_MS, DACRES_CONSUMO_MN, DACRES_CUSTOALIM_CAB
                FROM DET_AC_RESUMO_INATIVOS
                WHERE DACRES_DATA < CURRENT_DATE
            ) r
            JOIN CAD_ANIMAL a ON a.CA_CODIGO = r.COD_ANIMAL
        ) x
        GROUP BY x.COD_ANIMAL, x.COD_LOTE
        ORDER BY 1, 2
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        cod_animal = req_str(row.get("COD_ANIMAL"))
        cod_lote = req_str(row.get("COD_LOTE"))
        return {
            # identidade de negócio materializada — é o que a API ecoa em errors[]
            "CHAVE": f"{cod_animal}|{cod_lote}",
            "COD_ANIMAL": cod_animal,
            "COD_LOTE": cod_lote,
            "DATA_INICIAL": iso_date(row.get("DATA_INICIAL")),
            "DATA_FINAL": iso_date(row.get("DATA_FINAL")),
            # zero é dado aqui: animal x lote sem linha válida tem 0 dia e soma None
            "DIAS": integer(row.get("DIAS")),
            "CONSUMO_MS_KG": _soma(row.get("CONSUMO_MS_KG")),
            "CONSUMO_MN_KG": _soma(row.get("CONSUMO_MN_KG")),
            "CUSTO_ALIM": _soma(row.get("CUSTO_ALIM")),
            "DIAS_DESDE_ENTRADA": integer(row.get("DIAS_DESDE_ENTRADA")),
            "CONSUMO_MS_DESDE_ENTRADA_KG": _soma(row.get("CONSUMO_MS_DESDE_ENTRADA_KG")),
            "CONSUMO_MN_DESDE_ENTRADA_KG": _soma(row.get("CONSUMO_MN_DESDE_ENTRADA_KG")),
            "CUSTO_ALIM_DESDE_ENTRADA": _soma(row.get("CUSTO_ALIM_DESDE_ENTRADA")),
            "LINHAS": integer(row.get("LINHAS")),
            "LINHAS_DESCARTADAS": integer(row.get("LINHAS_DESCARTADAS")),
            "LINHAS_POS_SAIDA": integer(row.get("LINHAS_POS_SAIDA")),
        }
