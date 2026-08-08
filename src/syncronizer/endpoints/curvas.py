"""TGC `curvas` endpoint — curvas de crescimento/consumo para o AgroDB.

Source: Firebird 2.5 `DET_CATEGORIA` (catálogo, 95 linhas) + `DET_GMDPROJETADO`
        (GMD semanal, 3.270 linhas) + `DET_IMS_PROJ_CAT` (IMS diária, 3.450 linhas).
Target: POST /api/integracoes/tgc/curvas  body {"curvas": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda antes de `lotes` (order=14): o lote aponta a curva via CLL_COD_CURVA.

UM RECORD POR CURVA, AUTOCONTIDO — a curva só faz sentido inteira (o AgroDB projeta
peso somando a série), então cada record embute as duas séries como arrays ordenados
em vez de espelhar as 6,7k linhas-filhas como feeds próprios. O pipeline transforma
linha a linha, então a agregação acontece no próprio SQL: `LIST()` (Firebird 2.5)
em subselects escalares — subselect POR TABELA-FILHA de propósito, um join triplo
DET_CATEGORIA×GMD×IMS multiplicaria as listas (produto cartesiano por curva). O
LIST devolve BLOB SUB_TYPE TEXT, que o :func:`syncronizer.core.types.normalize` já
decoda para str; o transform faz o parse "chave:valor;..." e ORDENA — LIST não
garante ordem, a ordenação é responsabilidade do transform.

ARMADILHA — a FK real do IMS é `IMS_COD_DETCATEGORIA` (com underscore, nullable),
NÃO `IMS_CODDETCATEGORIA` (NOT NULL, parece a FK mas não é: só 6 valores distintos
que não batem com DET_CATEGORIA). Conferido na base real: pela coluna com underscore
o join fecha com 0 órfãos e 13 curvas distintas. Já o GMD usa `DGP_CODDETCATEGORIA`
(sem underscore) mesmo — 0 órfãos, 95 curvas.

Cobertura na base real: TODAS as 95 curvas têm GMD semanal; só 13 têm IMS diária.
`IMS_DIARIA: []` portanto é dado real (curva sem projeção de IMS), não bug.

Escalas: DGP_GMD é BIGINT scale -2 e IMS_VALOR_IMSPV scale -4; o `||` do Firebird
os renderiza já com a escala ("1:0.87", "1:1.8000") e o parse devolve float.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import BatchEndpoint, opt_str, req_str


def _serie(lista, campo_chave, campo_valor):
    """Parse de um LIST "chave:valor;chave:valor;..." em array ordenado de dicts.

    LIST() não garante ordem — a ordenação por chave acontece AQUI. Entradas
    malformadas (chave/valor não numéricos) são descartadas em vez de derrubar a
    curva inteira; None/vazio vira [] (curva sem a série é dado real).
    """
    if lista is None:
        return []
    if isinstance(lista, (bytes, bytearray)):  # defesa: normalize já decoda BLOB TEXT
        lista = bytes(lista).decode("utf-8", errors="replace")
    pontos = []
    for item in str(lista).split(";"):
        item = item.strip()
        if not item or ":" not in item:
            continue
        chave, _, valor = item.partition(":")
        try:
            pontos.append({campo_chave: int(chave.strip()),
                           campo_valor: float(valor.strip())})
        except (TypeError, ValueError):
            continue
    # Dedup por chave: 14 curvas do dado real tem cada semana CADASTRADA DUAS
    # VEZES no TGC (valores identicos, PKs distintas — ex. curva 59). A API do
    # AgroDB recusa a curva inteira com ponto duplicado; aqui vence a ULTIMA
    # ocorrencia na ordem do SELECT (maior PK = linha mais recente).
    unicos = {}
    for p in pontos:
        unicos[p[campo_chave]] = p
    pontos = sorted(unicos.values(), key=lambda p: p[campo_chave])
    return pontos


class CurvasEndpoint(BatchEndpoint):
    name = "curvas"                  # -> control table ep_curvas
    primary_key = "COD_CURVA"        # PK real do catálogo (DCAT_CODIGO)
    order = 14
    api_path = "/api/integracoes/tgc/curvas"
    api_method = "POST"

    # Full scan + row_hash — 95 curvas; um ponto editado na série muda o hash do
    # record inteiro e re-envia a curva completa, que é a unidade de consistência.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "curvas"
    record_key = "COD_CURVA"
    error_key = "cod_curva"

    # subselects escalares POR tabela-filha — nunca juntar GMD e IMS no mesmo FROM
    # (produto cartesiano multiplicaria as listas); FK do IMS é a COM underscore.
    _BASE_SQL = """
        SELECT
            dc.DCAT_CODIGO     AS COD_CURVA,
            r.CR_NOME          AS RACA,
            cat.CCAT_NOME      AS CATEGORIA,
            dc.DCAT_NOME_CURVA AS NOME,
            (SELECT LIST(g.DGP_SEMANA || ':' || g.DGP_GMD, ';')
               FROM DET_GMDPROJETADO g
              WHERE g.DGP_CODDETCATEGORIA = dc.DCAT_CODIGO) AS GMD_LISTA,
            (SELECT LIST(i.IMS_DIA || ':' || i.IMS_VALOR_IMSPV, ';')
               FROM DET_IMS_PROJ_CAT i
              WHERE i.IMS_COD_DETCATEGORIA = dc.DCAT_CODIGO) AS IMS_LISTA
        FROM DET_CATEGORIA dc
        LEFT JOIN CAD_RACA      r   ON r.CR_CODIGO     = dc.DCAT_CODRACA
        LEFT JOIN CAD_CATEGORIA cat ON cat.CCAT_CODIGO = dc.DCAT_CODCATEGORIA
        ORDER BY dc.DCAT_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_CURVA": req_str(row.get("COD_CURVA")),
            "RACA": opt_str(row.get("RACA")),
            "CATEGORIA": opt_str(row.get("CATEGORIA")),
            "NOME": opt_str(row.get("NOME")),
            # arrays SEMPRE ordenados aqui — LIST() não garante ordem
            "GMD_SEMANAL": _serie(row.get("GMD_LISTA"), "SEMANA", "GMD_KG_DIA"),
            "IMS_DIARIA": _serie(row.get("IMS_LISTA"), "DIA", "IMS_PV_PCT"),
        }
