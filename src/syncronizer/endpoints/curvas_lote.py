"""TGC `curvas_lote` endpoint — a curva de ganho COPIADA para cada lote.

Source: Firebird 2.5 `DET_GMDPROJ_LOTE` (40.740 linhas, 139 lotes na base de staging;
        uma linha por lote x dia, sem duplicata, até o dia 420).
Target: POST /api/integracoes/tgc/curvas-lote  body {"curvas_lote": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

POR QUE EXISTE — o feed `curvas` espelha o CATÁLOGO (`DET_GMDPROJETADO`, por raça x
categoria). O TGC não projeta por ele: na criação do lote a curva é copiada para
`DET_GMDPROJ_LOTE` (`CAD_LOTE_CRIAGMD`) e é essa cópia que entra no peso projetado
(`CNT_PESO_MEDIO_ATUAL`, `DACRES_PESO_MED_CLI`). O catálogo pode ser editado depois sem
mexer nos lotes — no backup de 09/09/2026 ele está inteiro em 1,35 kg/dia (3.270
linhas, um único valor) e os lotes seguem com 42 valores distintos de GMD. Projetar
pelo catálogo deixava o AgroDB ~5% abaixo do TGC em todo lote.

Roda logo depois de `lotes` (order=21).

UM RECORD POR LOTE, AUTOCONTIDO — mesma decisão do feed `curvas`: a curva só faz
sentido inteira, então o record embute a série como array ordenado e o destino
SUBSTITUI a curva do lote. A agregação é no SQL (`LIST()` com GROUP BY); o LIST não
garante ordem, a ordenação é do transform.

FULL SCAN + row_hash — 139 records; um dia editado muda o hash e re-envia a curva
inteira do lote, que é a unidade de consistência. ``reconcile_deletes`` fica False.

Escala: `DGP_GMD` é DECIMAL(18,2); o `||` do Firebird já o renderiza com a escala
("8:2.18") e o parse devolve float.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import BatchEndpoint, req_str
from syncronizer.endpoints.curvas import _serie


class CurvasLoteEndpoint(BatchEndpoint):
    name = "curvas_lote"             # -> control table ep_curvas_lote
    primary_key = "COD_LOTE"
    order = 21                       # logo depois de lotes (20)
    api_path = "/api/integracoes/tgc/curvas-lote"  # URL com hifen (padrao de rota do AgroDB)
    api_method = "POST"

    incremental_column = None
    reconcile_deletes = False

    payload_key = "curvas_lote"
    record_key = "COD_LOTE"
    error_key = "cod_lote"

    _BASE_SQL = """
        SELECT
            g.DGP_CODLOTE AS COD_LOTE,
            LIST(g.DGP_DIA || ':' || g.DGP_GMD, ';') AS GMD_LISTA
        FROM DET_GMDPROJ_LOTE g
        GROUP BY g.DGP_CODLOTE
        ORDER BY g.DGP_CODLOTE
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_LOTE": req_str(row.get("COD_LOTE")),
            # array SEMPRE ordenado por dia aqui — LIST() não garante ordem
            "GMD_DIARIA": _serie(row.get("GMD_LISTA"), "DIA", "GMD_KG_DIA"),
        }
