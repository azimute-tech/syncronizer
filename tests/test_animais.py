import json
from datetime import date
from decimal import Decimal

from syncronizer.endpoints.animais import AnimaisEndpoint


def _row(**over):
    base = {
        "COD_ANIMAL": 1, "LOTE_ENTRADA": 10001, "LOTE_ATUAL": 10020,
        "CURRAL_ATUAL": "ENF-1 ", "PESO_BALANCINHA": Decimal("315.00"),
        "RC_ENTRADA": Decimal("50.00"), "IDADE": 15, "DATA_ENTRADA": date(2026, 1, 28),
        "USUARIO": "FULANO", "NUM_CONTRATO": "1", "CATEGORIA": "BOI INTEIRO",
        "SISBOV": None, "CHIP": None, "SAIDA": "NENHUM",
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = AnimaisEndpoint()
    r = ep.transform(_row())
    assert r["COD_ANIMAL"] == "1"
    assert r["LOTE_ENTRADA"] == "10001" and r["LOTE_ATUAL"] == "10020"
    assert r["PESO_BALANCINHA"] == 315.0 and isinstance(r["PESO_BALANCINHA"], float)
    assert r["RC_ENTRADA"] == 50.0 and r["IDADE"] == 15
    assert r["DATA_ENTRADA"] == "2026-01-28"
    assert r["CURRAL_ATUAL"] == "ENF-1"  # CHAR padding stripped
    # NENHUM/None optional fields omitted (mirrors validated payload)
    assert "SAIDA" not in r and "SISBOV" not in r and "CHIP" not in r
    assert ep.make_pk(r) == "1"


def test_transform_keeps_real_exit_and_lote_fallback():
    ep = AnimaisEndpoint()
    r = ep.transform(_row(SAIDA="MORTE", LOTE_ATUAL=None))
    assert r["SAIDA"] == "MORTE"
    assert r["LOTE_ATUAL"] == "10001"  # falls back to LOTE_ENTRADA when null


def test_validate_rejects_bad_data():
    ep = AnimaisEndpoint()
    good = ep.transform(_row(DATA_ENTRADA=date(2020, 1, 1)))
    assert ep._validate(good) is None
    assert ep._validate({**good, "PESO_BALANCINHA": 0}) is not None
    assert ep._validate({**good, "RC_ENTRADA": 150}) is not None
    assert ep._validate({**good, "IDADE": 999}) is not None
    assert ep._validate({**good, "DATA_ENTRADA": "2999-01-01"}) is not None  # future
    assert ep._validate({**good, "USUARIO": ""}) is not None


class _Resp:
    def __init__(self, data):
        self._data = data
        self.content = b"{}"

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
    a = AnimaisEndpoint().transform(_row(COD_ANIMAL=cod, DATA_ENTRADA=date(2020, 1, 1)))
    return {"pk": str(cod), "payload": json.dumps(a), "deleted": False}


def test_send_batches_and_reconciles_per_item_errors():
    ep = AnimaisEndpoint()
    rows = [_unsent(1), _unsent(2), _unsent(3)]
    http = _Http({"inserted": 2, "updated": 0, "errors": [{"cod_animal": "3", "message": "dup"}]})
    res = ep.send(http, rows)
    assert set(res.ok) == {"1", "2"}
    assert [pk for pk, _ in res.failed] == ["3"]
    # one POST with the batch wrapped under "animais"
    assert len(http.calls) == 1
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/animais"
    assert isinstance(body["animais"], list) and len(body["animais"]) == 3


def test_send_drops_locally_invalid_without_sinking_batch():
    ep = AnimaisEndpoint()
    bad = {"pk": "9", "payload": json.dumps({"COD_ANIMAL": "9", "PESO_BALANCINHA": 0}), "deleted": False}
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(1), bad])
    assert "1" in res.ok
    assert any(pk == "9" for pk, _ in res.failed)
    assert len(http.calls[0][2]["animais"]) == 1  # only the valid one was POSTed
