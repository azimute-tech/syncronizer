"""Testes dos catálogos que dão NOME à dieta e ao ingrediente nos relatórios:
`racoes` (CAD_RACAO_PROD + CAD_RACAO, 47 linhas na staging) e `alimentos`
(CAD_ALIMENTO, 113 linhas).
"""
import json
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
