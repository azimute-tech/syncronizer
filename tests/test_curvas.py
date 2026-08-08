import json

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.curvas import CurvasEndpoint


def _row(**over):
    base = {
        "COD_CURVA": 21, "RACA": "NELORE ", "CATEGORIA": "BOI INTEIRO",
        "NOME": "NELORE-BOI INTEIRO",
        "GMD_LISTA": "1:0.87;2:2.42;3:2.27",
        "IMS_LISTA": "1:1.8000;2:1.8000",
    }
    base.update(over)
    return base


def test_transform_record_autocontido():
    ep = CurvasEndpoint()
    r = ep.transform(_row())
    assert r["COD_CURVA"] == "21" and ep.make_pk(r) == "21"
    assert r["RACA"] == "NELORE"            # padding de VARCHAR removido
    assert r["CATEGORIA"] == "BOI INTEIRO"
    assert r["NOME"] == "NELORE-BOI INTEIRO"
    assert r["GMD_SEMANAL"] == [
        {"SEMANA": 1, "GMD_KG_DIA": 0.87},
        {"SEMANA": 2, "GMD_KG_DIA": 2.42},
        {"SEMANA": 3, "GMD_KG_DIA": 2.27},
    ]
    assert r["IMS_DIARIA"] == [
        {"DIA": 1, "IMS_PV_PCT": 1.8},
        {"DIA": 2, "IMS_PV_PCT": 1.8},
    ]


def test_arrays_sao_ordenados_no_transform():
    """LIST() do Firebird NÃO garante ordem — a ordenação por SEMANA/DIA é
    responsabilidade do transform, senão a projeção de peso somaria a curva
    embaralhada."""
    ep = CurvasEndpoint()
    r = ep.transform(_row(GMD_LISTA="3:2.27;1:0.87;2:2.42",
                          IMS_LISTA="10:2.1000;2:1.8000;1:1.8000"))
    assert [p["SEMANA"] for p in r["GMD_SEMANAL"]] == [1, 2, 3]
    # ordenação numérica, não lexicográfica (10 depois de 2)
    assert [p["DIA"] for p in r["IMS_DIARIA"]] == [1, 2, 10]


def test_curva_sem_ims_e_dado_real():
    """Na base real TODAS as 95 curvas têm GMD mas só 13 têm IMS — lista NULL vira
    [] (curva sem projeção de IMS), nunca um erro."""
    ep = CurvasEndpoint()
    r = ep.transform(_row(IMS_LISTA=None))
    assert r["IMS_DIARIA"] == []
    assert len(r["GMD_SEMANAL"]) == 3       # a outra série não é afetada


def test_lista_em_bytes_e_itens_malformados():
    """Defesa: o LIST chega como BLOB TEXT (normalize decoda, mas bytes não podem
    quebrar) e um item malformado é descartado sem derrubar a curva inteira."""
    ep = CurvasEndpoint()
    r = ep.transform(_row(GMD_LISTA=b"2:1.25;1:0.90", IMS_LISTA="1:1.8;lixo;:;2:x;3:2.0"))
    assert r["GMD_SEMANAL"] == [
        {"SEMANA": 1, "GMD_KG_DIA": 0.9},
        {"SEMANA": 2, "GMD_KG_DIA": 1.25},
    ]
    assert r["IMS_DIARIA"] == [
        {"DIA": 1, "IMS_PV_PCT": 1.8},
        {"DIA": 3, "IMS_PV_PCT": 2.0},
    ]


def test_extract_spec_usa_a_fk_com_underscore_e_subselects():
    """A FK real do IMS é IMS_COD_DETCATEGORIA (com underscore, 0 órfãos na base);
    IMS_CODDETCATEGORIA parece a FK mas não é. E cada série agrega em subselect
    próprio — GMD e IMS no mesmo FROM multiplicariam as listas (produto
    cartesiano por curva)."""
    ep = CurvasEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "DET_CATEGORIA" in spec.sql and spec.params == ()
    assert "IMS_COD_DETCATEGORIA" in spec.sql
    assert "i.IMS_CODDETCATEGORIA" not in spec.sql   # a coluna-armadilha
    assert spec.sql.count("SELECT LIST") == 2        # um subselect por tabela-filha
    assert "DET_GMDPROJETADO" in spec.sql and "DET_IMS_PROJ_CAT" in spec.sql
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()           # Firebird usa FIRST/SKIP


class _Resp:
    content = b"{}"

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Http:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return _Resp(self.data)


def _unsent(cod):
    r = CurvasEndpoint().transform(_row(COD_CURVA=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_curvas_key_without_farm_id():
    ep = CurvasEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(21), _unsent(22)])
    assert set(res.ok) == {"21", "22"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/curvas"
    assert len(body["curvas"]) == 2
    # os arrays chegam intactos dentro do record
    assert body["curvas"][0]["GMD_SEMANAL"][0] == {"SEMANA": 1, "GMD_KG_DIA": 0.87}
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["curvas"])


def test_send_reconciles_per_item_errors():
    ep = CurvasEndpoint()
    http = _Http({"inserted": 1, "errors": [{"cod_curva": "22", "message": "raça inexistente"}]})
    res = ep.send(http, [_unsent(21), _unsent(22)])
    assert res.ok == ["21"] and [pk for pk, _ in res.failed] == ["22"]
