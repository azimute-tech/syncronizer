import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.controle_diario import ControleDiarioEndpoint


def _row(**over):
    base = {
        "COD_LOTE": 10138, "DATA": date(2026, 8, 5), "TIPO": "REBANHO",
        "CURRAL_NOME": "RMG-2",
        "CABECAS": Decimal("50.000000"), "MORTES": Decimal("0.000000"),
        "DIAS_CONF_MEDIO": Decimal("66.000000"),
        "PESO_ENTRADA_MEDIO": Decimal("263.780000"),
        "PESO_PROJETADO": Decimal("372.930000"),
        "GMD_MEDIO": Decimal("1.653788"),
        "CONSUMO_MS": Decimal("5.345028"), "CONSUMO_MN": Decimal("9.400000"),
        "IMS_PV": Decimal("1.351545"),
        "DATA_ABATE_PREVISTA": date(2026, 9, 9),
        "ARROBAS_PROJ": Decimal("9.053107"), "RC_PROJ": Decimal("53.000000"),
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = ControleDiarioEndpoint()
    r = ep.transform(_row())
    assert r["COD_LOTE"] == "10138" and r["DATA"] == "2026-08-05" and r["TIPO"] == "REBANHO"
    assert r["CURRAL_NOME"] == "RMG-2"
    assert r["CABECAS"] == 50 and isinstance(r["CABECAS"], int)
    assert r["MORTES"] == 0 and isinstance(r["MORTES"], int)
    assert r["DIAS_CONF_MEDIO"] == 66.0
    assert r["PESO_ENTRADA_MEDIO"] == 263.78
    assert r["PESO_PROJETADO"] == 372.93
    assert r["GMD_MEDIO"] == 1.653788
    assert r["CONSUMO_MS"] == 5.345028 and r["CONSUMO_MN"] == 9.4
    assert r["IMS_PV"] == 1.351545
    assert r["DATA_ABATE_PREVISTA"] == "2026-09-09"
    assert r["ARROBAS_PROJ"] == 9.053107 and r["RC_PROJ"] == 53.0


def test_chave_e_a_identidade_de_negocio():
    """CNT_CODIGO é PK física mas um re-cálculo do TGC recria a linha com outro
    código — a identidade de negócio é COD_LOTE|DATA|TIPO (única nas 6.847 linhas
    da base real): o espelho sobrescreve o dia, nunca duplica."""
    ep = ControleDiarioEndpoint()
    r = ep.transform(_row())
    assert r["CHAVE"] == "10138|2026-08-05|REBANHO"
    assert ep.make_pk(r) == "10138|2026-08-05|REBANHO"   # pk composta == CHAVE
    geral = ep.transform(_row(TIPO="GERAL"))
    assert geral["CHAVE"] == "10138|2026-08-05|GERAL"
    assert ep.make_pk(geral) != ep.make_pk(r)            # GERAL e REBANHO coexistem


def test_curral_e_o_nome_nunca_o_codigo():
    """CNT_CURRAL é VARCHAR com o NOME do curral ("B-1", "RMG-2"), não o código.
    Viaja como CURRAL_NOME — o mesmo precedente de movimentacoes; chamar de
    COD_CURRAL repetiria o bug silencioso de join documentado em animais."""
    ep = ControleDiarioEndpoint()
    r = ep.transform(_row(CURRAL_NOME="B-1"))
    assert r["CURRAL_NOME"] == "B-1"
    assert "COD_CURRAL" not in r
    assert ep.transform(_row(CURRAL_NOME=None))["CURRAL_NOME"] is None


def test_data_abate_prevista_e_a_oficial_e_efbio_nao_viaja():
    """CNT_DATA_ABATE_DIAS é a previsão OFICIAL da fazenda; as outras duas colunas de
    abate e o CNT_EFBIO (fora de escala, comprovado) não podem entrar no espelho."""
    ep = ControleDiarioEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CNT_DATA_ABATE_DIAS" in spec.sql
    assert "CNT_DATA_ABATE_BND" not in spec.sql
    assert "CNT_DATA_ABATE " not in spec.sql        # a coluna solta, sem sufixo
    assert "CNT_EFBIO" not in spec.sql
    r = ep.transform(_row(DATA_ABATE_PREVISTA=None))
    assert r["DATA_ABATE_PREVISTA"] is None          # lote ainda sem projeção


def test_extract_spec_filtra_d1_full_scan():
    """A linha do dia corrente é recalculada ao longo do dia — só o dia fechado
    espelha; e o TGC recalcula dias antigos, então é full scan + row_hash (um
    watermark por data perderia os re-cálculos)."""
    ep = ControleDiarioEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CONTROLE_DIARIO" in spec.sql and spec.params == ()
    assert "CNT_DATA < CURRENT_DATE" in spec.sql
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


def _unsent(lote, tipo="REBANHO"):
    ep = ControleDiarioEndpoint()
    r = ep.transform(_row(COD_LOTE=lote, TIPO=tipo))
    return {"pk": ep.make_pk(r), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_controle_diario_key_without_farm_id():
    ep = ControleDiarioEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(10138), _unsent(10138, tipo="GERAL")])
    assert set(res.ok) == {"10138|2026-08-05|REBANHO", "10138|2026-08-05|GERAL"}
    assert res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/controle-diario"
    assert len(body["controle_diario"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["controle_diario"])


def test_send_reconciles_per_item_errors_pela_chave():
    """A API ecoa a CHAVE composta em errors[].chave — é ela que casa o erro com o
    record certo (o mesmo lote aparece em vários dias no mesmo chunk)."""
    ep = ControleDiarioEndpoint()
    http = _Http({"inserted": 1, "errors": [
        {"chave": "10139|2026-08-05|REBANHO", "message": "lote inexistente"}]})
    res = ep.send(http, [_unsent(10138), _unsent(10139)])
    assert res.ok == ["10138|2026-08-05|REBANHO"]
    assert [pk for pk, _ in res.failed] == ["10139|2026-08-05|REBANHO"]
