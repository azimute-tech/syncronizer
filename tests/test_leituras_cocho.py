import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.leituras_cocho import LeiturasCochoEndpoint


def _row(**over):
    base = {
        "COD_LEITURA": 22965, "DATA": date(2026, 8, 5),
        "COD_CURRAL": 261, "COD_LOTE": 10103,
        "NOTA": Decimal("0.00"), "AJUSTE_KG_MS": Decimal("-0.080"),
        "KGMS_CAB_PREVISTO": 6.64, "KGMS_CAB_AJUSTADO": 6.56,
        "KGMS_CAB_REALIZADO": 6.54, "QTD_CABECAS": 149,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = LeiturasCochoEndpoint()
    r = ep.transform(_row())
    assert r["COD_LEITURA"] == "22965" and ep.make_pk(r) == "22965"
    assert r["DATA"] == "2026-08-05"
    assert r["COD_CURRAL"] == "261" and r["COD_LOTE"] == "10103"
    assert r["NOTA"] == 0.0
    assert r["AJUSTE_KG_MS"] == -0.08
    assert r["KGMS_CAB_PREVISTO"] == 6.64
    assert r["KGMS_CAB_AJUSTADO"] == 6.56
    assert r["KGMS_CAB_REALIZADO"] == 6.54
    assert r["QTD_CABECAS"] == 149 and isinstance(r["QTD_CABECAS"], int)


def test_nota_de_meio_ponto_nao_pode_virar_int():
    """A nota de cocho NÃO é inteira: a base real tem 288 leituras com 0,50 e 416 com
    1,50. Coagir para int arredondaria ~700 leituras em silêncio (0,5→0, 1,5→1) —
    a nota viaja como float."""
    ep = LeiturasCochoEndpoint()
    assert ep.transform(_row(NOTA=Decimal("0.50")))["NOTA"] == 0.5
    assert ep.transform(_row(NOTA=Decimal("1.50")))["NOTA"] == 1.5
    assert ep.transform(_row(NOTA=Decimal("-3.00")))["NOTA"] == -3.0
    assert ep.transform(_row(NOTA=Decimal("3.00")))["NOTA"] == 3.0


def test_nota_null_e_leitura_sem_nota():
    """628 leituras na base real não têm nota (NULL) — é leitura válida e viaja com
    NOTA None; o ajuste 0 é dado real ("manter") e viaja como 0.0."""
    ep = LeiturasCochoEndpoint()
    r = ep.transform(_row(NOTA=None, AJUSTE_KG_MS=Decimal("0.000")))
    assert r["NOTA"] is None
    assert r["AJUSTE_KG_MS"] == 0.0


def test_extract_spec_filtra_d1_full_scan():
    """A leitura dirige o ajuste do trato do dia — o dia corrente está em movimento
    e só o dia fechado espelha (mesmo racional do feed de fornecimentos)."""
    ep = LeiturasCochoEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_LEITURA" in spec.sql and spec.params == ()
    assert "CLC_DATALEITURA < CURRENT_DATE" in spec.sql
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


def test_curral_zero_sentinela_vira_none():
    r = LeiturasCochoEndpoint().transform(_row(COD_CURRAL=0))
    assert r["COD_CURRAL"] is None


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
    r = LeiturasCochoEndpoint().transform(_row(COD_LEITURA=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_leituras_cocho_key_without_farm_id():
    ep = LeiturasCochoEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(22964), _unsent(22965)])
    assert set(res.ok) == {"22964", "22965"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/leituras-cocho"
    assert len(body["leituras_cocho"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["leituras_cocho"])


def test_send_reconciles_per_item_errors():
    ep = LeiturasCochoEndpoint()
    http = _Http({"inserted": 1, "errors": [
        {"cod_leitura": "22965", "message": "curral inexistente"}]})
    res = ep.send(http, [_unsent(22964), _unsent(22965)])
    assert res.ok == ["22964"] and [pk for pk, _ in res.failed] == ["22965"]
