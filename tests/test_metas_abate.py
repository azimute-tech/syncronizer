import json
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.metas_abate import MetasAbateEndpoint


def _row(**over):
    base = {
        "DESTINO": "MÉDIO 520",
        "PESO_ALVO_KG_M": Decimal("520.0000"), "PESO_ALVO_KG_F": Decimal("460.0000"),
        "RC_PCT_M": Decimal("53.00"), "RC_PCT_F": Decimal("50.00"),
        "VALOR_ARROBA_M_RS": Decimal("89.00"), "VALOR_ARROBA_F_RS": Decimal("85.00"),
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = MetasAbateEndpoint()
    r = ep.transform(_row())
    # a PK real da tabela é o NOME (ADA_NOME) — é o que CLL_DESTINO referencia
    assert r["DESTINO"] == "MÉDIO 520" and ep.make_pk(r) == "MÉDIO 520"
    assert r["PESO_ALVO_KG_M"] == 520.0 and isinstance(r["PESO_ALVO_KG_M"], float)
    assert r["PESO_ALVO_KG_F"] == 460.0
    assert r["RC_PCT_M"] == 53.0 and r["RC_PCT_F"] == 50.0
    assert r["VALOR_ARROBA_M_RS"] == 89.0 and r["VALOR_ARROBA_F_RS"] == 85.0


def test_meta_e_sexada_nunca_achatada():
    """AUX_DESTINOANIMAL não tem 'peso alvo' único: cada destino carrega alvo, RC e
    R$/@ POR SEXO (colunas *M / *F no schema real). Achatar inventaria dado que a
    fonte não tem — o payload espelha os dois sexos."""
    spec = MetasAbateEndpoint().extract_spec(ExtractContext(last_watermark=None))
    for col in ("ADA_PESOABATEM", "ADA_PESOABATEF", "ADA_PESOABATERCM",
                "ADA_PESOABATERCF", "ADA_VALORARROBA_M", "ADA_VALORARROBA_F"):
        assert col in spec.sql


def test_extract_spec_is_a_full_scan():
    ep = MetasAbateEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "AUX_DESTINOANIMAL" in spec.sql and spec.params == ()
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


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


def _unsent(nome):
    r = MetasAbateEndpoint().transform(_row(DESTINO=nome))
    return {"pk": nome, "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_metas_abate_key_without_farm_id():
    ep = MetasAbateEndpoint()
    http = _Http({"inserted": 3, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent("LEVE 480"), _unsent("MÉDIO 520"), _unsent("PESADO 550")])
    assert set(res.ok) == {"LEVE 480", "MÉDIO 520", "PESADO 550"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/metas-abate"
    assert len(body["metas_abate"]) == 3
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["metas_abate"])


def test_send_reconciles_per_item_errors():
    ep = MetasAbateEndpoint()
    http = _Http({"inserted": 1, "errors": [{"destino": "LEVE 480", "message": "inválido"}]})
    res = ep.send(http, [_unsent("LEVE 480"), _unsent("MÉDIO 520")])
    assert res.ok == ["MÉDIO 520"] and [pk for pk, _ in res.failed] == ["LEVE 480"]
