import json
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.currais import CurraisEndpoint


def _row(**over):
    base = {
        "COD_CURRAL": 230, "NOME": "ENF-1 ", "TIPO": "CONFINAMENTO",
        "IS_HOSPITAL": "S", "LOTACAO_MAXIMA": 120, "LOTACAO_MINIMA": 1,
        "AREA_HA": Decimal("42.00"), "SETOR": None, "STATUS": "ATIVO",
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = CurraisEndpoint()
    r = ep.transform(_row())
    assert r["COD_CURRAL"] == "230" and ep.make_pk(r) == "230"
    assert r["NOME"] == "ENF-1"          # CHAR/VARCHAR padding stripped
    assert r["TIPO"] == "CONFINAMENTO"
    assert r["IS_HOSPITAL"] is True
    assert r["LOTACAO_MAXIMA"] == 120 and r["LOTACAO_MINIMA"] == 1
    assert r["AREA_HA"] == 42.0 and isinstance(r["AREA_HA"], float)
    assert r["SETOR"] is None and r["STATUS"] == "ATIVO"


def test_hospital_flag_is_a_real_boolean():
    ep = CurraisEndpoint()
    assert ep.transform(_row(IS_HOSPITAL="N"))["IS_HOSPITAL"] is False
    assert ep.transform(_row(IS_HOSPITAL=None))["IS_HOSPITAL"] is False
    assert ep.transform(_row(IS_HOSPITAL="s"))["IS_HOSPITAL"] is True


def test_pasto_and_confinamento_both_travel():
    """CC_TIPO is what tells AgroDB whether head sit on pasto or in a feedlot pen."""
    ep = CurraisEndpoint()
    assert ep.transform(_row(TIPO="PASTO"))["TIPO"] == "PASTO"
    assert ep.transform(_row(TIPO="CONFINAMENTO"))["TIPO"] == "CONFINAMENTO"


def test_extract_spec_is_a_full_scan():
    ep = CurraisEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_CURRAL" in spec.sql and spec.params == ()
    assert spec.incremental is False
    assert ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()      # Firebird uses FIRST/SKIP


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
    r = CurraisEndpoint().transform(_row(COD_CURRAL=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_currais_key_without_farm_id():
    ep = CurraisEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(212), _unsent(230)])
    assert set(res.ok) == {"212", "230"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/currais"
    assert len(body["currais"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["currais"])


def test_send_reconciles_per_item_errors():
    ep = CurraisEndpoint()
    http = _Http({"inserted": 1, "errors": [{"cod_curral": "230", "message": "nome duplicado"}]})
    res = ep.send(http, [_unsent(212), _unsent(230)])
    assert res.ok == ["212"] and [pk for pk, _ in res.failed] == ["230"]
