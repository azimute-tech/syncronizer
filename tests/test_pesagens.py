"""Testes do feed `pesagens` (DET_PESAGEM).

Os números citados nas docstrings foram medidos na base de staging real
(STAGING.fdb, 17/08/2026, 12.576 linhas).
"""
import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.pesagens import PesagensEndpoint


def _row(**over):
    base = {
        "COD_PESAGEM": 46872, "COD_ANIMAL": 2400, "TIPO": "SAIDA",
        "DATA": date(2026, 7, 28), "PESO_KG": Decimal("450.00"),
        "CARCACA_KG": Decimal("239.6422"), "ARROBAS": Decimal("15.9761"),
        "RENDIMENTO_PCT": Decimal("53.25"), "CLASSIFICACAO": None,
        "DENTICAO": None, "GORDURA": None, "SCORE": None, "HORAS_JEJUM": None,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = PesagensEndpoint()
    r = ep.transform(_row())
    assert r["COD_PESAGEM"] == "46872" and ep.make_pk(r) == "46872"
    assert r["COD_ANIMAL"] == "2400"
    assert r["TIPO"] == "SAIDA"
    assert r["DATA"] == "2026-07-28"
    assert r["PESO_KG"] == 450.0 and isinstance(r["PESO_KG"], float)
    assert r["CARCACA_KG"] == 239.6422 and r["ARROBAS"] == 15.9761
    assert r["RENDIMENTO_PCT"] == 53.25


def test_carcaca_e_arroba_zero_viram_none_juntas():
    """As 5 linhas com DP_CARCACA 0,0000 na base real são abate SEM carcaça informada:
    DP_ARROBA é 0 e DP_RENDIMENTO é NULL nas MESMAS 5 linhas. Arroba é carcaça/15 na
    própria origem, então deixar ARROBAS em 0.0 com CARCACA_KG em None faria o destino
    fechar o abate com "0 @" reais em vez de "não informado"."""
    ep = PesagensEndpoint()
    r = ep.transform(_row(CARCACA_KG=Decimal("0.0000"), ARROBAS=Decimal("0.0000"),
                          RENDIMENTO_PCT=None))
    assert r["CARCACA_KG"] is None and r["ARROBAS"] is None
    assert r["RENDIMENTO_PCT"] is None


def test_peso_vivo_zero_seria_dado_real():
    """Peso é peso: 0 não é sentinela de "não informado" e tem que chegar ao destino
    para ele enxergar o problema (não ocorre na base atual, mas a regra é essa)."""
    assert PesagensEndpoint().transform(_row(PESO_KG=Decimal("0.00")))["PESO_KG"] == 0.0


def test_denticao_zero_do_tgc_vira_none():
    """DIVERGÊNCIA do contrato: DP_DENTICAO é INTEGER na origem (o contrato o descreve
    como TEXT) e as 551 linhas preenchidas trazem 0, o sentinela "não informado" do
    TGC. Passa por opt_code: 0 -> None, valor real -> texto."""
    ep = PesagensEndpoint()
    assert ep.transform(_row(DENTICAO=0))["DENTICAO"] is None
    assert ep.transform(_row(DENTICAO=4))["DENTICAO"] == "4"


def test_score_inteiro_vira_float():
    """DP_SCORE é INTEGER 1..3 na origem e NUMERIC(6,2) no contrato."""
    r = PesagensEndpoint().transform(_row(SCORE=3))
    assert r["SCORE"] == 3.0 and isinstance(r["SCORE"], float)


def test_rc_impossivel_viaja_sem_gate_local():
    """A base real tem RC até 110,52 (impossível). Não há quality gate local em lugar
    nenhum do agente: a linha viaja para o destino ver o problema, a correção acontece
    no TGC e o row_hash re-envia."""
    r = PesagensEndpoint().transform(_row(RENDIMENTO_PCT=Decimal("110.52")))
    assert r["RENDIMENTO_PCT"] == 110.52


def test_full_scan_e_nao_incremental():
    """Decisão CONTRA a linha "Incremental por DP_CODIGO" do contrato: 9.011 das 12.576
    linhas da staging têm DP_DATA_UPDATE posterior a DP_DATAREG, inclusive 1.021 do
    tipo SAIDA num único dia — exatamente a carcaça/RC do fechamento. Um watermark pela
    PK não veria correção nenhuma (o id não muda) e congelaria o dado errado."""
    ep = PesagensEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and spec.incremental is False
    assert ep.reconcile_deletes is False
    assert "DET_PESAGEM" in spec.sql and spec.params == ()
    assert "WHERE" not in spec.sql.upper()
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


def _unsent(cod):
    r = PesagensEndpoint().transform(_row(COD_PESAGEM=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_pesagens_key_without_farm_id():
    ep = PesagensEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(46871), _unsent(46872)])
    assert set(res.ok) == {"46871", "46872"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/pesagens"
    assert len(body["pesagens"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["pesagens"])


def test_send_reconciles_per_item_errors():
    ep = PesagensEndpoint()
    http = _Http({"inserted": 1, "errors": [
        {"cod_pesagem": "46872", "message": "animal inexistente"}]})
    res = ep.send(http, [_unsent(46871), _unsent(46872)])
    assert res.ok == ["46871"] and [pk for pk, _ in res.failed] == ["46872"]
