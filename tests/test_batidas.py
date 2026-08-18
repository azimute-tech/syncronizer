"""Testes dos feeds da fábrica de ração: `batidas` (CAD_BATIDA, 1.691 linhas na
staging) e `batidas_itens` (DET_BATIDA, 8.484 linhas).
"""
import json
from datetime import date, time
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.batidas import BatidasEndpoint
from syncronizer.endpoints.batidas_itens import BatidasItensEndpoint


def _batida(**over):
    base = {
        "COD_BATIDA": 2173, "COD_RACAO_PROD": 10068, "DATA": date(2026, 8, 13),
        "QTDE_PREVISTA_KG": Decimal("1099.9700"),
        "QTDE_REALIZADA_KG": Decimal("1088.0000"),
        "QTD_CABECAS": None, "CUSTO_RS": Decimal("735.1644"),
        "HORA_INICIO": time(0, 0), "HORA_FIM": time(0, 0),
        "OPERADOR": "PASEIRO", "MOTORISTA": None, "TIPO": "CONSUMO",
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = BatidasEndpoint()
    r = ep.transform(_batida())
    assert r["COD_BATIDA"] == "2173" and ep.make_pk(r) == "2173"
    assert r["COD_RACAO_PROD"] == "10068"
    assert r["DATA"] == "2026-08-13"
    assert r["QTDE_PREVISTA_KG"] == 1099.97 and r["QTDE_REALIZADA_KG"] == 1088.0
    assert r["CUSTO_RS"] == 735.1644 and isinstance(r["CUSTO_RS"], float)
    assert r["OPERADOR"] == "PASEIRO" and r["TIPO"] == "CONSUMO"


def test_horas_viram_texto_hhmm():
    """CBT_INICIO/CBT_FIM são TIME e o driver devolve datetime.time; o contrato pede
    texto HH:MM (segundos não significam nada num horário digitado à mão)."""
    ep = BatidasEndpoint()
    r = ep.transform(_batida(HORA_INICIO=time(7, 30, 12), HORA_FIM=time(9, 5)))
    assert r["HORA_INICIO"] == "07:30" and r["HORA_FIM"] == "09:05"
    assert ep.transform(_batida(HORA_INICIO=None))["HORA_INICIO"] is None


def test_hora_zero_viaja_como_esta():
    """DIVERGÊNCIA: CBT_INICIO/CBT_FIM são 00:00:00 em 1.691/1.691 linhas da staging —
    o default intocado do TIME. O contrato não define regra de ausência para hora, e
    não há quality gate local em lugar nenhum do agente: "00:00" viaja e é o DESTINO
    que decide lê-lo como "não informado"."""
    r = BatidasEndpoint().transform(_batida())
    assert r["HORA_INICIO"] == "00:00" and r["HORA_FIM"] == "00:00"


def test_kg_zero_e_dado_real_mas_custo_zero_vira_none():
    """kg 0 é batida prevista e não executada (dado real); CBT_CUSTO 0,0000 é o default
    "não informado" do TGC e vira None — mesmo racional do CUSTO_KG_MN em
    `fornecimentos`."""
    ep = BatidasEndpoint()
    r = ep.transform(_batida(QTDE_REALIZADA_KG=Decimal("0.0000"),
                             QTDE_PREVISTA_KG=Decimal("0.0000"),
                             CUSTO_RS=Decimal("0.0000")))
    assert r["QTDE_REALIZADA_KG"] == 0.0 and r["QTDE_PREVISTA_KG"] == 0.0
    assert r["CUSTO_RS"] is None
    assert ep.transform(_batida(CUSTO_RS=None))["CUSTO_RS"] is None


def test_colunas_vazias_na_staging_ainda_assim_existem():
    """CBT_QTDECABECA e CBT_NOME_MOTORISTA são NULL em 1.691/1.691 linhas. As colunas
    entram prontas para quando a fazenda passar a preencher — sem elas o dado novo
    seria descartado em silêncio (mesmo racional do VALOR_ENTRADA em `animais`)."""
    r = BatidasEndpoint().transform(_batida())
    assert "QTD_CABECAS" in r and r["QTD_CABECAS"] is None
    assert "MOTORISTA" in r and r["MOTORISTA"] is None
    assert BatidasEndpoint().transform(_batida(QTD_CABECAS=108))["QTD_CABECAS"] == 108


def test_extract_spec_full_scan_sem_filtro_d1():
    """Full scan pelo mesmo motivo de `fornecimentos` (CBT_DATA_UPDATE existe e está
    preenchida em 1.691/1.691: a batida É corrigida depois de gravada).

    Mas SEM o filtro D-1 de `fornecimentos`/`leituras_cocho`: lá a linha é um agregado
    do dia que só fecha à meia-noite; aqui cada linha é um evento discreto de mistura,
    completo assim que é gravado."""
    ep = BatidasEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and spec.incremental is False
    assert "CAD_BATIDA" in spec.sql and spec.params == ()
    assert "CURRENT_DATE" not in spec.sql.upper()
    assert "WHERE" not in spec.sql.upper()
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


def test_flag_fim_nao_e_usada_como_filtro():
    """CBT_FLAG_FIM seria a trava natural de "batida terminada", mas está em 'N' nas
    1.691 linhas da staging — nunca vira 'S' nesta base, então não serve de filtro."""
    spec = BatidasEndpoint().extract_spec(ExtractContext(last_watermark=None))
    assert "CBT_FLAG_FIM" not in spec.sql


# --- batidas_itens ------------------------------------------------------------

def _item(**over):
    base = {
        "COD_ITEM": 11746, "COD_BATIDA": 2173, "COD_ALIMENTO": 80013,
        "DATA": date(2026, 8, 13), "PREVISTO_KG": Decimal("418.7300"),
        "REALIZADO_KG": Decimal("426.0000"), "PREV_KG_MS": Decimal("18.0000"),
        "REAL_KG_MS": Decimal("18.6929"), "MS_PCT": None,
        "CUSTO_RS": Decimal("0.1907"),
    }
    base.update(over)
    return base


def test_itens_transform_shape_and_types():
    ep = BatidasItensEndpoint()
    r = ep.transform(_item())
    assert r["COD_ITEM"] == "11746" and ep.make_pk(r) == "11746"
    assert r["COD_BATIDA"] == "2173" and r["COD_ALIMENTO"] == "80013"
    assert r["DATA"] == "2026-08-13"
    assert r["PREVISTO_KG"] == 418.73 and r["REALIZADO_KG"] == 426.0
    assert r["PREV_KG_MS"] == 18.0 and r["REAL_KG_MS"] == 18.6929
    # DBT_CUSTO é o custo UNITÁRIO do alimento (R$/kg), não o total da linha:
    # SUM(DBT_CUSTO x DBT_QTDE) é que reproduz o CBT_CUSTO do cabeçalho.
    assert r["CUSTO_RS"] == 0.1907


def test_itens_kg_zero_e_dado_real_mas_custo_zero_vira_none():
    """101 linhas da staging têm DBT_QTDE 0,0000 — ingrediente previsto e não colocado
    na mistura, informação legítima de aderência à dieta. Já as 90 linhas com
    DBT_CUSTO 0,0000 são "não informado"."""
    ep = BatidasItensEndpoint()
    r = ep.transform(_item(REALIZADO_KG=Decimal("0.0000"), CUSTO_RS=Decimal("0.0000")))
    assert r["REALIZADO_KG"] == 0.0
    assert r["CUSTO_RS"] is None


def test_ms_pct_vazio_na_staging_ainda_assim_existe():
    """DIVERGÊNCIA: DBT_MS é NULL em 8.484/8.484. A coluna entra pronta; o %MS efetivo
    pode ser derivado no destino por REAL_KG_MS / REALIZADO_KG."""
    ep = BatidasItensEndpoint()
    r = ep.transform(_item())
    assert "MS_PCT" in r and r["MS_PCT"] is None
    assert ep.transform(_item(MS_PCT=Decimal("56.86")))["MS_PCT"] == 56.86


def test_itens_rodam_depois_do_cabecalho():
    assert BatidasEndpoint().order < BatidasItensEndpoint().order


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


def test_send_batidas_usa_a_chave_do_contrato():
    ep = BatidasEndpoint()
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    rec = ep.transform(_batida())
    res = ep.send(http, [{"pk": "2173", "payload": json.dumps(rec), "deleted": False}])
    method, path, body = http.calls[0]
    assert res.ok == ["2173"]
    assert method == "POST" and path == "/api/integracoes/tgc/batidas"
    assert list(body) == ["batidas"] and "farm_id" not in body


def test_send_itens_usa_payload_key_itens():
    """O contrato usa payload_key `itens` (não `batidas_itens`) nesta rota."""
    ep = BatidasItensEndpoint()
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    rec = ep.transform(_item())
    res = ep.send(http, [{"pk": "11746", "payload": json.dumps(rec), "deleted": False}])
    method, path, body = http.calls[0]
    assert res.ok == ["11746"]
    assert method == "POST" and path == "/api/integracoes/tgc/batidas-itens"
    assert list(body) == ["itens"] and "farm_id" not in body
