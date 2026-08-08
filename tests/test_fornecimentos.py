import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.fornecimentos import FornecimentosEndpoint


def _row(**over):
    base = {
        "COD_FORNECIMENTO": 41021, "DATA": date(2026, 8, 5),
        "COD_LOTE": 10132, "COD_CURRAL": 252, "TRATO": 4,
        "PREVISTO_KG": Decimal("252.0691"), "REALIZADO_KG": Decimal("290.0000"),
        "SOBRA_KG": Decimal("0.0000"), "QTD_CABECAS": 108,
        "MS_PCT": Decimal("56.8620"), "CUSTO_KG_MN": Decimal("0.5679"),
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = FornecimentosEndpoint()
    r = ep.transform(_row())
    assert r["COD_FORNECIMENTO"] == "41021" and ep.make_pk(r) == "41021"
    assert r["DATA"] == "2026-08-05"
    assert r["COD_LOTE"] == "10132" and r["COD_CURRAL"] == "252"
    assert r["TRATO"] == 4 and isinstance(r["TRATO"], int)
    assert r["PREVISTO_KG"] == 252.0691 and isinstance(r["PREVISTO_KG"], float)
    assert r["REALIZADO_KG"] == 290.0
    assert r["QTD_CABECAS"] == 108 and isinstance(r["QTD_CABECAS"], int)
    assert r["MS_PCT"] == 56.862
    assert r["CUSTO_KG_MN"] == 0.5679


def test_kg_zero_e_dado_real_mas_custo_zero_vira_none():
    """kg 0 é trato previsto e não fornecido (2.443 linhas na base real) e VIAJA como
    0.0; custo 0,0000 é o default "não informado" do TGC (1.838 linhas) e vira None —
    encaminhá-lo plantaria custo alimentar falso de R$ 0/kg."""
    ep = FornecimentosEndpoint()
    r = ep.transform(_row(REALIZADO_KG=Decimal("0.0000"), SOBRA_KG=Decimal("0.0000"),
                          CUSTO_KG_MN=Decimal("0.0000")))
    assert r["REALIZADO_KG"] == 0.0 and r["SOBRA_KG"] == 0.0     # dado real
    assert r["CUSTO_KG_MN"] is None                              # não informado
    assert ep.transform(_row(CUSTO_KG_MN=None))["CUSTO_KG_MN"] is None
    # custo preenchido viaja como float
    assert ep.transform(_row(CUSTO_KG_MN=Decimal("0.5679")))["CUSTO_KG_MN"] == 0.5679


def test_data_e_do_fornecimento_nunca_a_de_registro():
    """CFORN_DATAFORNECIDO é a data do trato; CFORN_DATAREG é quando foi digitado
    (114 linhas divergem na base real — registro na manhã seguinte). Usar a data de
    registro jogaria o consumo no dia errado. Não existe coluna CFORN_DATA."""
    spec = FornecimentosEndpoint().extract_spec(ExtractContext(last_watermark=None))
    assert "CFORN_DATAFORNECIDO" in spec.sql
    assert "CFORN_DATAREG" not in spec.sql


def test_extract_spec_filtra_d1_full_scan():
    """O dia corrente fica de fora (tratos 1..4 lançados ao longo do dia + sobra que
    só fecha no dia seguinte): espelhar o dia aberto enviaria consumo parcial."""
    ep = FornecimentosEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_FORNECIMENTO" in spec.sql and spec.params == ()
    assert "CFORN_DATAFORNECIDO < CURRENT_DATE" in spec.sql
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


def test_curral_zero_sentinela_vira_none():
    r = FornecimentosEndpoint().transform(_row(COD_CURRAL=0))
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
    r = FornecimentosEndpoint().transform(_row(COD_FORNECIMENTO=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_fornecimentos_key_without_farm_id():
    ep = FornecimentosEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(41020), _unsent(41021)])
    assert set(res.ok) == {"41020", "41021"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/fornecimentos"
    assert len(body["fornecimentos"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["fornecimentos"])


def test_send_reconciles_per_item_errors():
    ep = FornecimentosEndpoint()
    http = _Http({"inserted": 1, "errors": [
        {"cod_fornecimento": "41021", "message": "lote inexistente"}]})
    res = ep.send(http, [_unsent(41020), _unsent(41021)])
    assert res.ok == ["41020"] and [pk for pk, _ in res.failed] == ["41021"]
