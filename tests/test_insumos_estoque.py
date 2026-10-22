"""Testes dos feeds do ESTOQUE de insumos (Relatório de Insumos do AgroDB):
`insumos_entradas` (DET_ITENS_NFINSUMOS x CAD_NFINSUMOS, 361 cargas na staging) e
`insumos_baixas` (CAD_BAIXA_ALIM, 115 baixas).
"""
import json
from datetime import date, time
from decimal import Decimal

from syncronizer.core import registry
from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.alimentos import AlimentosEndpoint
from syncronizer.endpoints.batidas import BatidasEndpoint
from syncronizer.endpoints.batidas_itens import BatidasItensEndpoint
from syncronizer.endpoints.insumos_baixas import OBS_MAX, InsumosBaixasEndpoint
from syncronizer.endpoints.insumos_entradas import InsumosEntradasEndpoint


def _entrada(**over):
    # carga 909 da staging (milho grão úmido), com ticket da fazenda e umidade/impureza
    base = {
        "COD_ENTRADA": 909, "COD_NF": 881, "COD_COMPRA": 37, "COD_ALIMENTO": 80026,
        "NUMERO_NF": "185", "SERIE_NF": "",
        "DATA_EMISSAO": date(2026, 7, 2), "DATA_ENTREGA": date(2026, 7, 3),
        "DATA_RECEBIMENTO": date(2026, 7, 3), "HORA_RECEBIMENTO": time(10, 0),
        "DATA_LANCAMENTO": date(2026, 7, 20), "LANCADA_ESTOQUE": "S",
        "FORNECEDOR": "AGROWIRRIS COMERCIO E INDUSTRIA LTDA", "PLACA": "",
        "TICKET_FAZENDA": 811,
        "QTDE_KG": Decimal("75400.00"), "PESO_NF_KG": Decimal("75560.00"),
        "VALOR_UNIT_RS_T": Decimal("1002.12"), "VALOR_TOTAL_RS": Decimal("75560.00"),
        "UMIDADE_PCT": Decimal("11.00"), "IMPUREZA_PCT": Decimal("0.90"),
    }
    base.update(over)
    return base


CHAVES_ENTRADA = [
    "COD_ENTRADA", "COD_NF", "COD_COMPRA", "COD_ALIMENTO", "NUMERO_NF", "SERIE_NF",
    "DATA_EMISSAO", "DATA_ENTREGA", "DATA_RECEBIMENTO", "HORA_RECEBIMENTO",
    "DATA_LANCAMENTO", "LANCADA_ESTOQUE", "FORNECEDOR", "PLACA", "TICKET_FAZENDA",
    "QTDE_KG", "PESO_NF_KG", "VALOR_UNIT_RS_T", "VALOR_TOTAL_RS", "UMIDADE_PCT",
    "IMPUREZA_PCT",
]


def test_entradas_payload_tem_exatamente_as_chaves_do_contrato():
    """Contrato normativo (seção 12): a rota do AgroDB é escrita contra estes nomes."""
    assert list(InsumosEntradasEndpoint().transform(_entrada())) == CHAVES_ENTRADA


def test_entradas_transform_shape_and_types():
    ep = InsumosEntradasEndpoint()
    r = ep.transform(_entrada())
    assert r["COD_ENTRADA"] == "909" and ep.make_pk(r) == "909"
    assert r["COD_NF"] == "881" and r["COD_COMPRA"] == "37"
    assert r["COD_ALIMENTO"] == "80026" and r["NUMERO_NF"] == "185"
    assert r["DATA_EMISSAO"] == "2026-07-02" and r["DATA_ENTREGA"] == "2026-07-03"
    assert r["DATA_RECEBIMENTO"] == "2026-07-03" and r["HORA_RECEBIMENTO"] == "10:00"
    assert r["DATA_LANCAMENTO"] == "2026-07-20" and r["LANCADA_ESTOQUE"] is True
    assert r["QTDE_KG"] == 75400.0 and isinstance(r["QTDE_KG"], float)
    assert r["PESO_NF_KG"] == 75560.0
    # R$ por TONELADA recebida: 75.560 / 75,4 t
    assert r["VALOR_UNIT_RS_T"] == 1002.12 and r["VALOR_TOTAL_RS"] == 75560.0
    assert r["UMIDADE_PCT"] == 11.0 and r["IMPUREZA_PCT"] == 0.9


def test_entradas_qtde_zero_e_dado_real_e_ausente_viaja_none():
    """QTDE_KG é o peso que entra no saldo: 0 é dado real; NULL viaja None e é o
    DESTINO que recusa a linha (sem quality gate local)."""
    ep = InsumosEntradasEndpoint()
    assert ep.transform(_entrada(QTDE_KG=Decimal("0.00")))["QTDE_KG"] == 0.0
    assert ep.transform(_entrada(QTDE_KG=None))["QTDE_KG"] is None


def test_entradas_regra_de_zero():
    """0 é o default "não informado/não medido" do TGC: peso da NF, valores, umidade
    (0 em 355/361) e impureza (0 em 357/361) viram None."""
    r = InsumosEntradasEndpoint().transform(_entrada(
        PESO_NF_KG=Decimal("0.00"), VALOR_UNIT_RS_T=Decimal("0.00"),
        VALOR_TOTAL_RS=Decimal("0.00"), UMIDADE_PCT=Decimal("0.00"),
        IMPUREZA_PCT=Decimal("0.00"),
    ))
    for campo in ("PESO_NF_KG", "VALOR_UNIT_RS_T", "VALOR_TOTAL_RS",
                  "UMIDADE_PCT", "IMPUREZA_PCT"):
        assert r[campo] is None, campo
    r = InsumosEntradasEndpoint().transform(_entrada(UMIDADE_PCT=None, PESO_NF_KG=None))
    assert r["UMIDADE_PCT"] is None and r["PESO_NF_KG"] is None


def test_entradas_lancada_estoque_e_booleano_do_flag_contab():
    """DNFI_FLAG_CONTAB = 'S' — mesmo predicado da Posição do TGC. A carga 1125 da
    staging está 'N' (recebida e não lançada) e conta no saldo do AgroDB."""
    ep = InsumosEntradasEndpoint()
    assert ep.transform(_entrada(LANCADA_ESTOQUE="S"))["LANCADA_ESTOQUE"] is True
    r = ep.transform(_entrada(LANCADA_ESTOQUE="N", DATA_LANCAMENTO=None))
    assert r["LANCADA_ESTOQUE"] is False and r["DATA_LANCAMENTO"] is None
    assert ep.transform(_entrada(LANCADA_ESTOQUE=None))["LANCADA_ESTOQUE"] is False


def test_entradas_hora_recebimento_vira_hhmm():
    """CNFI_HORA_REC é TIME; o contrato pede HH:MM. Segundos caem (01:04:06 existe na
    staging) e 00:00 viaja como está, como em `batidas`."""
    ep = InsumosEntradasEndpoint()
    assert ep.transform(_entrada(HORA_RECEBIMENTO=time(1, 4, 6)))["HORA_RECEBIMENTO"] == "01:04"
    assert ep.transform(_entrada(HORA_RECEBIMENTO=time(0, 0)))["HORA_RECEBIMENTO"] == "00:00"
    assert ep.transform(_entrada(HORA_RECEBIMENTO=None))["HORA_RECEBIMENTO"] is None


def test_entradas_numero_nf_e_texto():
    """CNFI_NUMERO é texto livre: 44 cargas da staging não são um número."""
    ep = InsumosEntradasEndpoint()
    for numero in ("1872-1877", "FAKE2", "NOTA FAKE", ",304", "115"):
        assert ep.transform(_entrada(NUMERO_NF=numero))["NUMERO_NF"] == numero


def test_entradas_vazios_viram_none():
    """PLACA e SERIE_NF estão vazias em 361/361 ('' ou NULL); o ticket da fazenda é
    INTEGER no TGC, TEXT no espelho, e 0 = sem ticket."""
    ep = InsumosEntradasEndpoint()
    r = ep.transform(_entrada(PLACA="", SERIE_NF=None, TICKET_FAZENDA=0))
    assert r["PLACA"] is None and r["SERIE_NF"] is None and r["TICKET_FAZENDA"] is None
    assert ep.transform(_entrada(TICKET_FAZENDA=None))["TICKET_FAZENDA"] is None
    assert ep.transform(_entrada())["TICKET_FAZENDA"] == "811"
    assert ep.transform(_entrada(PLACA="ABC1D23"))["PLACA"] == "ABC1D23"


def test_entradas_texto_com_acento_sobrevive_ao_json():
    ep = InsumosEntradasEndpoint()
    r = ep.transform(_entrada(FORNECEDOR="JS GRÃOS ARMAZÉNS GERAIS LTDA"))
    assert r["FORNECEDOR"] == "JS GRÃOS ARMAZÉNS GERAIS LTDA"
    assert json.loads(json.dumps(r))["FORNECEDOR"] == "JS GRÃOS ARMAZÉNS GERAIS LTDA"
    assert ep.transform(_entrada(FORNECEDOR=None))["FORNECEDOR"] is None


def test_entradas_extract_spec_full_scan_com_joins():
    """Full scan: a carga é lançada no estoque dias depois de recebida, com o mesmo
    DNFI_CODIGO. Cabeçalho por JOIN; compra e fornecedor por LEFT JOIN, para uma
    compra apagada não sumir com o peso recebido."""
    ep = InsumosEntradasEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and ep.reconcile_deletes is False
    assert spec.incremental is False and spec.params == ()
    sql = " ".join(spec.sql.split())
    assert "FROM DET_ITENS_NFINSUMOS d JOIN CAD_NFINSUMOS n" in sql
    assert "LEFT JOIN CAD_COMPRAINSUMOS c" in sql
    assert "LEFT JOIN CAD_FORNECEDOR f" in sql
    assert "WHERE" not in sql.upper()
    assert "LIMIT" not in sql.upper()           # Firebird usa FIRST/SKIP


# --- insumos_baixas -----------------------------------------------------------

def _baixa(**over):
    base = {
        "COD_BAIXA": 39, "COD_ALIMENTO": 80026, "DATA": date(2026, 8, 27),
        "QTDE_KG": Decimal("601300.0000"), "TIPO": "SAIDA",
        "CENTRO_CUSTO": "AGROPEC VITÓRIA 2",
        "OBS": "DEVOLUÇÃO DE MILHO PARA FAZENDA NASCENTE",
    }
    base.update(over)
    return base


def test_baixas_payload_tem_exatamente_as_chaves_do_contrato():
    assert list(InsumosBaixasEndpoint().transform(_baixa())) == [
        "COD_BAIXA", "COD_ALIMENTO", "DATA", "QTDE_KG", "TIPO", "CENTRO_CUSTO", "OBS",
    ]


def test_baixas_transform_shape_and_types():
    ep = InsumosBaixasEndpoint()
    r = ep.transform(_baixa())
    assert r["COD_BAIXA"] == "39" and ep.make_pk(r) == "39"
    assert r["COD_ALIMENTO"] == "80026" and r["DATA"] == "2026-08-27"
    assert r["QTDE_KG"] == 601300.0 and isinstance(r["QTDE_KG"], float)
    assert r["TIPO"] == "SAIDA"
    assert r["CENTRO_CUSTO"] == "AGROPEC VITÓRIA 2"
    assert r["OBS"] == "DEVOLUÇÃO DE MILHO PARA FAZENDA NASCENTE"


def test_baixas_qtde_zero_e_dado_real():
    """A baixa 27 da staging é um CONSUMO de 0 kg — viaja como 0.0."""
    assert InsumosBaixasEndpoint().transform(_baixa(QTDE_KG=Decimal("0.0000")))["QTDE_KG"] == 0.0


def test_baixas_obs_trim_vazio_e_none():
    ep = InsumosBaixasEndpoint()
    assert ep.transform(_baixa(OBS=None))["OBS"] is None
    assert ep.transform(_baixa(OBS=""))["OBS"] is None
    assert ep.transform(_baixa(OBS="  \r\n "))["OBS"] is None
    assert ep.transform(_baixa(OBS="  perda no silo  "))["OBS"] == "perda no silo"


def test_baixas_obs_preserva_quebra_de_linha_interna_e_acento():
    obs = "Faltou 500 kg na baixa de estoque do dia.\r\nErro de lançamento"
    assert InsumosBaixasEndpoint().transform(_baixa(OBS=obs))["OBS"] == obs


def test_baixas_obs_truncada_em_500():
    ep = InsumosBaixasEndpoint()
    r = ep.transform(_baixa(OBS="ç" * 800))
    assert OBS_MAX == 500 and r["OBS"] == "ç" * 500
    assert ep.transform(_baixa(OBS="a" * 500))["OBS"] == "a" * 500
    # o corte não deixa espaço pendurado no fim
    assert ep.transform(_baixa(OBS="a" * 499 + " " + "b" * 10))["OBS"] == "a" * 499


def test_baixas_obs_em_bytes_e_decodificada():
    """O driver já devolve o BLOB de texto como str (conferido na staging com UTF8 e
    WIN1252). Defesa para charset NONE: bytes UTF-8 ou WIN1252 viram texto certo."""
    ep = InsumosBaixasEndpoint()
    assert ep.transform(_baixa(OBS="grão úmido".encode("utf-8")))["OBS"] == "grão úmido"
    assert ep.transform(_baixa(OBS="grão úmido".encode("cp1252")))["OBS"] == "grão úmido"


def test_baixas_centro_custo_ausente_nao_some_com_a_linha():
    ep = InsumosBaixasEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "LEFT JOIN CAD_CENTROCUSTO cc" in spec.sql
    assert ep.transform(_baixa(CENTRO_CUSTO=None))["CENTRO_CUSTO"] is None


def test_baixas_extract_spec_full_scan():
    ep = InsumosBaixasEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and ep.reconcile_deletes is False
    assert spec.incremental is False and spec.params == ()
    assert "FROM CAD_BAIXA_ALIM b" in spec.sql
    assert "WHERE" not in spec.sql.upper()
    assert "LIMIT" not in spec.sql.upper()


# --- ordem e registro ---------------------------------------------------------

def test_estoque_roda_depois_do_catalogo_e_das_batidas():
    """O insumo da carga e da baixa aponta para AA_CODIGO (alimentos, order=20); o
    plano põe o estoque depois das batidas (55/56)."""
    entradas, baixas = InsumosEntradasEndpoint(), InsumosBaixasEndpoint()
    assert AlimentosEndpoint().order < entradas.order
    assert AlimentosEndpoint().order < baixas.order
    assert BatidasEndpoint().order < BatidasItensEndpoint().order < entradas.order
    assert entradas.order < baixas.order


def test_registry_descobre_os_feeds_do_estoque():
    nomes = [e.name for e in registry.discover()]
    assert "insumos_entradas" in nomes and "insumos_baixas" in nomes
    assert nomes.index("alimentos") < nomes.index("insumos_entradas")
    assert nomes.index("batidas_itens") < nomes.index("insumos_entradas")
    assert nomes.index("insumos_entradas") < nomes.index("insumos_baixas")


# --- send ---------------------------------------------------------------------

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


def test_send_usa_rota_e_payload_key_do_contrato():
    for ep, rec, pk, path, key in (
        (InsumosEntradasEndpoint(), _entrada(), "909",
         "/api/integracoes/tgc/insumos-entradas", "entradas"),
        (InsumosBaixasEndpoint(), _baixa(), "39",
         "/api/integracoes/tgc/insumos-baixas", "baixas"),
    ):
        http = _Http({"inserted": 1, "updated": 0, "errors": []})
        payload = json.dumps(ep.transform(rec))
        res = ep.send(http, [{"pk": pk, "payload": payload, "deleted": False}])
        method, called, body = http.calls[0]
        assert res.ok == [pk] and res.failed == []
        assert method == "POST" and called == path
        assert list(body) == [key] and "farm_id" not in body


def test_send_casa_erro_por_item_pela_chave():
    """A API do AgroDB ecoa o item rejeitado em errors[].chave (TgcIngestError
    {index, chave, message}); a linha recusada fica para o próximo ciclo e as demais
    são marcadas enviadas."""
    ep = InsumosEntradasEndpoint()
    boa = ep.transform(_entrada())
    ruim = ep.transform(_entrada(COD_ENTRADA=1125, QTDE_KG=None))
    http = _Http({"inserted": 1, "updated": 0, "errors": [
        {"index": 1, "chave": "1125", "message": "QTDE_KG ausente"}]})
    res = ep.send(http, [
        {"pk": "909", "payload": json.dumps(boa), "deleted": False},
        {"pk": "1125", "payload": json.dumps(ruim), "deleted": False},
    ])
    assert res.ok == ["909"]
    assert res.failed == [("1125", "api: QTDE_KG ausente")]

    ep = InsumosBaixasEndpoint()
    http = _Http({"inserted": 0, "updated": 0, "errors": [
        {"index": 0, "chave": "39", "message": "COD_ALIMENTO inexistente"}]})
    res = ep.send(http, [{"pk": "39", "payload": json.dumps(ep.transform(_baixa())),
                          "deleted": False}])
    assert res.ok == [] and res.failed == [("39", "api: COD_ALIMENTO inexistente")]
