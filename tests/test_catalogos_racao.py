"""Testes dos catálogos que dão NOME à dieta e ao ingrediente nos relatórios:
`racoes` (CAD_RACAO_PROD + CAD_RACAO, 47 linhas na staging) e `alimentos`
(CAD_ALIMENTO, 113 linhas).
"""
import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.alimentos import AlimentosEndpoint
from syncronizer.endpoints.batidas import BatidasEndpoint
from syncronizer.endpoints.batidas_itens import BatidasItensEndpoint
from syncronizer.endpoints.fornecimentos import FornecimentosEndpoint
from syncronizer.endpoints.racoes import RacoesEndpoint


def _racao(**over):
    base = {"COD_RACAO_PROD": 10022, "COD_RACAO": 31,
            "NOME": "DIETA TIP_27/01", "STATUS": "I"}
    base.update(over)
    return base


def test_racoes_transform_shape():
    ep = RacoesEndpoint()
    r = ep.transform(_racao())
    assert r["COD_RACAO_PROD"] == "10022" and ep.make_pk(r) == "10022"
    assert r["COD_RACAO"] == "31"
    assert r["NOME"] == "DIETA TIP_27/01"


def test_status_da_racao_e_ai_nao_sn():
    """CRP_STATUS é 'A'/'I' (ativo/inativo — 3 e 44 na staging), NÃO o 'S'/'N' de flag
    booleana dos outros feeds. Tratá-lo como boolean transformaria toda ração ativa em
    False."""
    ep = RacoesEndpoint()
    assert ep.transform(_racao(STATUS="A"))["STATUS"] == "A"
    assert ep.transform(_racao(STATUS="I"))["STATUS"] == "I"


def test_join_e_left_para_nome_ausente_nao_sumir_a_linha():
    """Uma versão produzida apontando para fórmula inexistente tem que viajar com NOME
    nulo, para o destino enxergar a falha em vez de a linha desaparecer."""
    ep = RacoesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "LEFT JOIN CAD_RACAO r" in spec.sql
    assert ep.transform(_racao(NOME=None))["NOME"] is None


def test_racoes_full_scan_sem_reconcile():
    """47 linhas; renomear a dieta precisa re-enviar (row_hash). Uma ração sai de
    circulação virando STATUS='I', não sendo deletada — nada a tombstonear."""
    ep = RacoesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None and ep.reconcile_deletes is False
    assert "CAD_RACAO_PROD" in spec.sql and spec.params == ()
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


# --- alimentos ----------------------------------------------------------------

def _alimento(**over):
    base = {"COD_ALIMENTO": 2, "NOME": "AVEIA (FENO)", "TIPO": "VOLUMOSO",
            "CUSTO_MEDIO_RS": Decimal("0.0000")}
    base.update(over)
    return base


def test_alimentos_transform_shape():
    ep = AlimentosEndpoint()
    r = ep.transform(_alimento(CUSTO_MEDIO_RS=Decimal("0.5679")))
    assert r["COD_ALIMENTO"] == "2" and ep.make_pk(r) == "2"
    assert r["NOME"] == "AVEIA (FENO)"
    assert r["TIPO"] == "VOLUMOSO"
    assert r["CUSTO_MEDIO_RS"] == 0.5679


def test_custo_medio_zero_vira_none():
    """77 das 113 linhas da staging têm AA_CUSTOMEDIO 0,0000 — ingrediente cadastrado e
    nunca comprado. Encaminhar 0 criaria um ingrediente que "custa R$ 0/kg" e zeraria o
    custo alimentar de toda dieta que o usa."""
    ep = AlimentosEndpoint()
    assert ep.transform(_alimento())["CUSTO_MEDIO_RS"] is None
    assert ep.transform(_alimento(CUSTO_MEDIO_RS=None))["CUSTO_MEDIO_RS"] is None


def test_alimentos_full_scan():
    """O custo médio é recalculado a cada compra (a origem mantém AA_DATAULTCUSTO e
    AA_DATA_UPDATE por isso): um watermark por AA_CODIGO congelaria nome, tipo e custo
    no estado do cadastro inicial."""
    ep = AlimentosEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert ep.incremental_column is None
    assert "CAD_ALIMENTO" in spec.sql and spec.params == ()
    assert "LIMIT" not in spec.sql.upper()


# --- alimentos: campos do estoque (contrato seção 12) -------------------------

def _alimento_estoque(**over):
    # MILHO MOÍDO da staging: entra na posição, mínimo de 20 dias, sem mínimo em kg
    base = _alimento(
        COD_ALIMENTO=80038, NOME="MILHO MOÍDO_NOVO 07/08", TIPO="ENERGETICO",
        CUSTO_MEDIO_RS=Decimal("1.0580"), CONTROLA_ESTOQUE="S",
        ESTOQUE_MINIMO_KG=Decimal("0.00"), ESTOQUE_MINIMO_DIAS=20,
        MS_PCT=Decimal("88.00"), UNIDADE="KG", DATA_ULT_CUSTO=None,
    )
    base.update(over)
    return base


def test_alimentos_payload_ganha_os_seis_campos_do_estoque():
    r = AlimentosEndpoint().transform(_alimento_estoque())
    assert list(r) == [
        "COD_ALIMENTO", "NOME", "TIPO", "CUSTO_MEDIO_RS",
        "CONTROLA_ESTOQUE", "ESTOQUE_MINIMO_KG", "ESTOQUE_MINIMO_DIAS", "MS_PCT",
        "UNIDADE", "DATA_ULT_CUSTO",
    ]
    assert r["NOME"] == "MILHO MOÍDO_NOVO 07/08"
    assert r["CONTROLA_ESTOQUE"] is True
    assert r["ESTOQUE_MINIMO_DIAS"] == 20 and isinstance(r["ESTOQUE_MINIMO_DIAS"], int)
    assert r["COD_ALIMENTO"] == "80038" and r["CUSTO_MEDIO_RS"] == 1.058
    assert r["ESTOQUE_MINIMO_KG"] is None    # 0,00 em 137/137 na staging
    assert r["MS_PCT"] == 88.0 and r["UNIDADE"] == "KG"


def test_controla_estoque_e_rel_estoque_atual_igual_a_s():
    """Mesmo predicado da Posição do TGC (`AA_REL_ESTOQUE_ATUAL = 'S'`): 'N' (8 silos
    encerrados na staging) e NULL ficam fora."""
    ep = AlimentosEndpoint()
    assert ep.transform(_alimento_estoque(CONTROLA_ESTOQUE="N"))["CONTROLA_ESTOQUE"] is False
    assert ep.transform(_alimento_estoque(CONTROLA_ESTOQUE=None))["CONTROLA_ESTOQUE"] is False


def test_estoque_minimo_e_ms_regra_de_zero():
    """AA_ESTOQUEMINIMO é 0 em 137/137 (ninguém cadastrou mínimo em kg); 0 dias e MS
    0,00 (AGUA-PADRAO) também são "não cadastrado". MS 100,00 dos minerais é real."""
    ep = AlimentosEndpoint()
    r = ep.transform(_alimento_estoque(ESTOQUE_MINIMO_DIAS=0, MS_PCT=Decimal("0.00")))
    assert r["ESTOQUE_MINIMO_KG"] is None
    assert r["ESTOQUE_MINIMO_DIAS"] is None and r["MS_PCT"] is None
    r = ep.transform(_alimento_estoque(ESTOQUE_MINIMO_DIAS=None, MS_PCT=None,
                                       ESTOQUE_MINIMO_KG=None))
    assert r["ESTOQUE_MINIMO_DIAS"] is None and r["MS_PCT"] is None
    assert r["ESTOQUE_MINIMO_KG"] is None
    r = ep.transform(_alimento_estoque(ESTOQUE_MINIMO_KG=Decimal("5000.00"),
                                       MS_PCT=Decimal("100.00")))
    assert r["ESTOQUE_MINIMO_KG"] == 5000.0 and r["MS_PCT"] == 100.0


def test_data_ult_custo_e_unidade():
    """DIVERGÊNCIA: AA_DATAULTCUSTO é NULL em 137/137 na staging — a coluna entra pronta."""
    ep = AlimentosEndpoint()
    r = ep.transform(_alimento_estoque())
    assert "DATA_ULT_CUSTO" in r and r["DATA_ULT_CUSTO"] is None
    r = ep.transform(_alimento_estoque(DATA_ULT_CUSTO=date(2026, 10, 15), UNIDADE=""))
    assert r["DATA_ULT_CUSTO"] == "2026-10-15" and r["UNIDADE"] is None


def test_alimentos_sql_le_os_campos_do_estoque():
    spec = AlimentosEndpoint().extract_spec(ExtractContext(last_watermark=None))
    for coluna in ("AA_REL_ESTOQUE_ATUAL", "AA_ESTOQUEMINIMO", "AA_ESTOQUE_MIN_DIAS",
                   "AA_MS", "AA_UN_MEDIDA", "AA_DATAULTCUSTO"):
        assert coluna in spec.sql, coluna


# --- ordenação entre feeds ----------------------------------------------------

def test_catalogos_rodam_antes_de_quem_os_referencia():
    """`fornecimentos` e `batidas` apontam para CRP_CODIGO; `batidas_itens` aponta para
    AA_CODIGO. Os catálogos precisam ter sido enviados antes no mesmo ciclo."""
    racoes, alimentos = RacoesEndpoint(), AlimentosEndpoint()
    assert racoes.order < FornecimentosEndpoint().order
    assert racoes.order < BatidasEndpoint().order
    assert alimentos.order < BatidasItensEndpoint().order


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


def test_send_racoes_e_alimentos_usam_as_chaves_do_contrato():
    for ep, rec, pk, path, key in (
        (RacoesEndpoint(), _racao(), "10022", "/api/integracoes/tgc/racoes", "racoes"),
        (AlimentosEndpoint(), _alimento(), "2",
         "/api/integracoes/tgc/alimentos", "alimentos"),
    ):
        http = _Http({"inserted": 1, "updated": 0, "errors": []})
        payload = json.dumps(ep.transform(rec))
        res = ep.send(http, [{"pk": pk, "payload": payload, "deleted": False}])
        method, called, body = http.calls[0]
        assert res.ok == [pk]
        assert method == "POST" and called == path
        assert list(body) == [key] and "farm_id" not in body
