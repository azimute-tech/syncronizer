import json
import re
from datetime import date

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.lotes import LotesEndpoint


def _row(**over):
    base = {
        "COD_LOTE": 10001, "COD_CURRAL": 212, "TIPO_EXPLORACAO": "TIP",
        "STATUS": "A", "QTD_CABECAS": 301, "DIAS_CONFINAMENTO_ALVO": 100,
        "DATA_MEDIA_ENTRADA": date(2026, 3, 10), "DATA_FIM": date(2026, 6, 29),
        "NUM_CONTRATO": None,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = LotesEndpoint()
    r = ep.transform(_row())
    assert r["COD_LOTE"] == "10001" and ep.make_pk(r) == "10001"
    assert r["COD_CURRAL"] == "212"
    assert r["TIPO_EXPLORACAO"] == "TIP"
    assert r["STATUS"] == "A"
    assert r["QTD_CABECAS"] == 301 and r["DIAS_CONFINAMENTO_ALVO"] == 100
    assert r["DATA_MEDIA_ENTRADA"] == "2026-03-10" and r["DATA_FIM"] == "2026-06-29"
    assert r["NUM_CONTRATO"] is None


def test_phase_comes_from_tipo_exploracao_not_tipo():
    """CLL_TIPO is the ownership regime ('PRÓPRIO'); the phase is CLL_TIPO_EXPLORACAO.
    A row carrying CLL_TIPO must not leak into the payload."""
    ep = LotesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CLL_TIPO_EXPLORACAO" in spec.sql
    # CLL_TIPO must never be read on its own
    assert re.search(r"CLL_TIPO(?!_EXPLORACAO)", spec.sql) is None
    for fase in ("RECRIA", "TIP", "SEMICONFINAMENTO", "CONFINAMENTO"):
        assert ep.transform(_row(TIPO_EXPLORACAO=fase))["TIPO_EXPLORACAO"] == fase


def test_zero_curral_sentinel_becomes_null():
    """A closed lote carries CLL_CODCURRAL = 0 and there is no curral 0 — forwarding it
    would create a dangling reference at the destination."""
    r = LotesEndpoint().transform(_row(COD_CURRAL=0, STATUS="I"))
    assert r["COD_CURRAL"] is None and r["STATUS"] == "I"


def test_char_padding_is_stripped():
    r = LotesEndpoint().transform(_row(STATUS="A ", TIPO_EXPLORACAO="RECRIA  "))
    assert r["STATUS"] == "A" and r["TIPO_EXPLORACAO"] == "RECRIA"


def test_extract_spec_is_a_full_scan():
    ep = LotesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_LOTE" in spec.sql and spec.params == ()
    assert spec.incremental is False and ep.incremental_column is None
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
    r = LotesEndpoint().transform(_row(COD_LOTE=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_lotes_key_without_farm_id():
    ep = LotesEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(10001), _unsent(10002)])
    assert set(res.ok) == {"10001", "10002"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/lotes"
    assert len(body["lotes"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["lotes"])


def test_send_reconciles_per_item_errors():
    ep = LotesEndpoint()
    http = _Http({"inserted": 1, "errors": [{"cod_lote": "10002", "message": "curral inexistente"}]})
    res = ep.send(http, [_unsent(10001), _unsent(10002)])
    assert res.ok == ["10001"] and [pk for pk, _ in res.failed] == ["10002"]
