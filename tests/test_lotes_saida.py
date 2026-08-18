"""Testes dos feeds do fechamento de abate: `lotes_saida` (CAD_LOTESAIDA, 48 linhas na
staging) e `lotes_saida_custos` (DET_LOTESAIDA_FIN, 576 linhas).
"""
import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.lotes_saida import LotesSaidaEndpoint
from syncronizer.endpoints.lotes_saida_custos import LotesSaidaCustosEndpoint


def _lote(**over):
    base = {
        "COD_LOTE_SAIDA": 3, "COD_DESTINO": 6,
        "DATA_ABATE": date(2026, 5, 5), "DATA_EMBARQUE": date(2026, 5, 4),
        "QTD_CABECAS": 110, "CONF_PESO_KG": Decimal("62121.00"), "CONF_QTD_CAB": 110,
        "PORT_PESO_KG": Decimal("0.00"), "FRIG_PESO_KG": Decimal("0.00"),
        "FRIG_QTD_CAB": 0, "KG_TOTAL_CARCACA": Decimal("33346.55"),
        "TOTAL_ARROBA": Decimal("2223.10"), "RC_PCT": Decimal("53.68"),
        "VALOR_BRUTO": Decimal("770507.66"), "VALOR_LIQUIDO": Decimal("770507.66"),
        "VALOR_IMPOSTOS": Decimal("0.00"), "NUMERO_NF": None,
        "VALOR_NF": Decimal("0.00"), "DATA_PAGTO_PREV": None, "DATA_PAGTO_PAGO": None,
        "CUSTO_DIARIA": Decimal("0.00"), "NUM_CONTRATO": None, "FECHADO": "S",
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = LotesSaidaEndpoint()
    r = ep.transform(_lote())
    assert r["COD_LOTE_SAIDA"] == "3" and ep.make_pk(r) == "3"
    assert r["COD_DESTINO"] == "6"
    assert r["DATA_ABATE"] == "2026-05-05" and r["DATA_EMBARQUE"] == "2026-05-04"
    assert r["QTD_CABECAS"] == 110 and isinstance(r["QTD_CABECAS"], int)
    assert r["CONF_PESO_KG"] == 62121.0 and r["KG_TOTAL_CARCACA"] == 33346.55
    assert r["RC_PCT"] == 53.68 and r["VALOR_BRUTO"] == 770507.66


def test_valores_zerados_do_tgc_viram_none():
    """Na staging CLS_VALOR_IMPOSTOS, CLS_VALOR_NF e CLS_CUSTODIARIA são 0,00 em 48/48
    e CLS_VALORBRUTO/LIQUIDO em 6/48: é o default "não informado" do TGC. Encaminhar 0
    plantaria receita falsa de R$ 0 num campo que o relatório usa como FALLBACK do
    romaneio do AgroDB."""
    r = LotesSaidaEndpoint().transform(
        _lote(VALOR_BRUTO=Decimal("0.00"), VALOR_LIQUIDO=Decimal("0.00")))
    assert r["VALOR_BRUTO"] is None and r["VALOR_LIQUIDO"] is None
    assert r["VALOR_IMPOSTOS"] is None and r["VALOR_NF"] is None
    assert r["CUSTO_DIARIA"] is None


def test_peso_frigorifico_zero_vira_none_mas_os_outros_pesos_nao():
    """Exceção explícita do contrato: FRIG_PESO_KG 0 é "comum" e vira None (48/48 na
    staging). CONF_PESO_KG e PORT_PESO_KG são pesos e 0 viaja como dado real — mesmo
    que PORT_PESO_KG esteja 0 em 44/48 (divergência documentada no módulo: estender a
    regra a ele é decisão do contrato, não deste transform)."""
    r = LotesSaidaEndpoint().transform(_lote(CONF_PESO_KG=Decimal("0.00")))
    assert r["FRIG_PESO_KG"] is None
    assert r["PORT_PESO_KG"] == 0.0
    assert r["CONF_PESO_KG"] == 0.0


def test_flag_fechado_vira_boolean():
    ep = LotesSaidaEndpoint()
    assert ep.transform(_lote(FECHADO="S"))["FECHADO"] is True
    assert ep.transform(_lote(FECHADO="N"))["FECHADO"] is False
    assert ep.transform(_lote(FECHADO=None))["FECHADO"] is False


def test_destino_zero_sentinela_vira_none():
    assert LotesSaidaEndpoint().transform(_lote(COD_DESTINO=0))["COD_DESTINO"] is None


def test_extract_spec_full_scan():
    """O lote nasce no embarque e recebe peso de porteira, carcaça, NF e pagamento por
    semanas; não existe CLS_DATA_UPDATE para watermark e um watermark por CLS_CODIGO
    congelaria o lote no estado do embarque."""
    ep = LotesSaidaEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and spec.incremental is False
    assert "CAD_LOTESAIDA" in spec.sql and spec.params == ()
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


# --- lotes_saida_custos -------------------------------------------------------

def _custo(**over):
    base = {
        "COD_CUSTO": 1777, "COD_LOTE_SAIDA": 3, "DESCRICAO": "BONIFICACOES COURO",
        "VALOR": Decimal("0.00"), "STATUS": "R", "PORC_VALOR": Decimal("0.00"),
        "IMPOSTO": "N",
    }
    base.update(over)
    return base


def test_custos_transform_shape():
    ep = LotesSaidaCustosEndpoint()
    r = ep.transform(_custo(VALOR=Decimal("770507.66"),
                            PORC_VALOR=Decimal("100.00"),
                            DESCRICAO="VENDA ANIMAIS"))
    assert r["COD_CUSTO"] == "1777" and ep.make_pk(r) == "1777"
    assert r["COD_LOTE_SAIDA"] == "3"
    assert r["DESCRICAO"] == "VENDA ANIMAIS"
    assert r["VALOR"] == 770507.66
    assert r["STATUS"] == "R"               # CHAR(1) 'R'/'D', não 'S'/'N'
    assert r["PORC_VALOR"] == 100.0
    assert r["IMPOSTO"] is False


def test_rubrica_do_template_nao_lancada_vira_none():
    """DET_LOTESAIDA_FIN é um template de 12 rubricas por lote (12 x 48 = 576). Na
    staging só VENDA ANIMAIS está preenchida: 534 linhas têm 0,00. Mandá-las como R$
    0,00 afirmaria "não houve comissão/ICMS/FUNRURAL" em vez de "não foi informado"."""
    r = LotesSaidaCustosEndpoint().transform(_custo())
    assert r["VALOR"] is None
    # o percentual NÃO é R$: segue numérico, e o destino vê a ausência pelo VALOR nulo
    assert r["PORC_VALOR"] == 0.0


def test_flag_imposto_vira_boolean():
    ep = LotesSaidaCustosEndpoint()
    assert ep.transform(_custo(IMPOSTO="S"))["IMPOSTO"] is True
    assert ep.transform(_custo(IMPOSTO="N"))["IMPOSTO"] is False


def test_custos_rodam_depois_do_lote():
    """A rubrica referencia lotes_saida_tgc.cod_lote_saida: o cabeçalho tem que ter
    sido enviado antes no mesmo ciclo."""
    assert LotesSaidaEndpoint().order < LotesSaidaCustosEndpoint().order


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


def test_send_lotes_saida_usa_a_chave_do_contrato():
    ep = LotesSaidaEndpoint()
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    rec = ep.transform(_lote())
    res = ep.send(http, [{"pk": "3", "payload": json.dumps(rec), "deleted": False}])
    method, path, body = http.calls[0]
    assert res.ok == ["3"]
    assert method == "POST" and path == "/api/integracoes/tgc/lotes-saida"
    assert list(body) == ["lotes_saida"] and "farm_id" not in body


def test_send_custos_usa_payload_key_custos():
    """O contrato usa payload_key `custos` (não `lotes_saida_custos`) nesta rota."""
    ep = LotesSaidaCustosEndpoint()
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    rec = ep.transform(_custo())
    res = ep.send(http, [{"pk": "1777", "payload": json.dumps(rec), "deleted": False}])
    method, path, body = http.calls[0]
    assert res.ok == ["1777"]
    assert method == "POST" and path == "/api/integracoes/tgc/lotes-saida-custos"
    assert list(body) == ["custos"] and "farm_id" not in body
