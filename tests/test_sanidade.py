"""Testes do feed `sanidade` (DET_SANIDADE).

A base de staging tem ZERO aplicações — a estrutura e a ingestão entram porque a
produção do cliente tem o dado. Consequência: não há como MEDIR aqui se a premissa
"append-only" se sustenta, como foi possível medir (e refutar) em DET_PESAGEM; estes
testes fixam o contrato do transform e da máquina de watermark.
"""
import json
from datetime import date

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.sanidade import SanidadeEndpoint


def _row(**over):
    base = {
        "DSANI_CODIGO": 501, "COD_ANIMAL": 2400, "SISBOV": "105123456789012",
        "DATA_APLICACAO": date(2026, 5, 5), "COD_PRODUTO": 77,
        "TIPO": "VACINA", "MOTIVO": "PROTOCOLO ENTRADA",
        "DOSE_ML": 5.0, "VALOR_APLICACAO": 3.75,
        "DATA_CARENCIA": date(2026, 6, 5), "COD_PROTOCOLO": 12,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = SanidadeEndpoint()
    r = ep.transform(_row())
    assert r["COD_SANIDADE"] == "501" and ep.make_pk(r) == "501"
    assert r["COD_ANIMAL"] == "2400"
    assert r["SISBOV"] == "105123456789012"
    assert r["DATA_APLICACAO"] == "2026-05-05" and r["DATA_CARENCIA"] == "2026-06-05"
    assert r["COD_PRODUTO"] == "77" and r["COD_PROTOCOLO"] == "12"
    assert r["TIPO"] == "VACINA" and r["MOTIVO"] == "PROTOCOLO ENTRADA"
    assert r["DOSE_ML"] == 5.0 and r["VALOR_APLICACAO"] == 3.75


def test_dose_zero_e_dado_real_mas_valor_zero_vira_none():
    """Dose é medida (mL): 0 é dado real. Valor é R$ e o 0 do TGC é "não informado" —
    o custo sanitário do fechamento tem que sair como "não informado" em vez de R$ 0."""
    r = SanidadeEndpoint().transform(_row(DOSE_ML=0.0, VALOR_APLICACAO=0.0))
    assert r["DOSE_ML"] == 0.0
    assert r["VALOR_APLICACAO"] is None


def test_fks_zeradas_do_tgc_viram_none():
    """0 é o sentinela de "sem relacionado" nas FKs INTEGER do TGC; encaminhá-lo criaria
    referência pendurada no destino."""
    r = SanidadeEndpoint().transform(_row(COD_ANIMAL=0, COD_PRODUTO=0, COD_PROTOCOLO=0))
    assert r["COD_ANIMAL"] is None
    assert r["COD_PRODUTO"] is None and r["COD_PROTOCOLO"] is None


def test_sisbov_destruido_pelo_excel_e_descartado():
    """Mesma regra de `animais`: notação científica não é identificador de baixa
    qualidade, é um identificador DESTRUÍDO (ver _common.clean_id)."""
    assert SanidadeEndpoint().transform(_row(SISBOV="9,63E+14"))["SISBOV"] is None


def test_extract_spec_first_run_reads_everything():
    ep = SanidadeEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "DET_SANIDADE" in spec.sql and spec.params == ()
    assert spec.incremental is False
    assert "WHERE" not in spec.sql.upper()
    assert "LIMIT" not in spec.sql.upper()      # Firebird usa FIRST/SKIP


def test_extract_spec_incremental_uses_qmark_and_numeric_param():
    """O watermark vai e volta como TEXT no SQLite de controle; tem que ser religado
    como o inteiro que a coluna realmente é, com placeholder qmark."""
    ep = SanidadeEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark="501"))
    assert spec.incremental is True
    assert "WHERE s.DSANI_CODIGO > ?" in spec.sql
    assert ":" not in spec.sql.split("SELECT", 1)[1]  # sem binds :named
    assert spec.params == (501,)
    # a coluna do watermark é selecionada SEM alias, senão o row.get("DSANI_CODIGO")
    # do orquestrador devolve None e o watermark nunca avança
    assert ep.incremental_column == "DSANI_CODIGO"
    assert "s.DSANI_CODIGO,\n" in spec.sql


def test_incremental_never_reconciles_deletes():
    """Tombstone só é válido depois de um full scan; a partir de um delta apagaria tudo
    que não veio no último delta, ou seja, tudo."""
    assert SanidadeEndpoint().reconcile_deletes is False


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


def test_send_posts_batch_under_sanidade_key_without_farm_id():
    ep = SanidadeEndpoint()
    http = _Http({"inserted": 1, "updated": 0, "errors": []})
    rec = ep.transform(_row())
    res = ep.send(http, [{"pk": "501", "payload": json.dumps(rec), "deleted": False}])
    method, path, body = http.calls[0]
    assert res.ok == ["501"]
    assert method == "POST" and path == "/api/integracoes/tgc/sanidade"
    assert list(body) == ["sanidade"] and "farm_id" not in body
    assert all("farm_id" not in item for item in body["sanidade"])


def test_send_sem_linhas_nao_faz_requisicao():
    """Com a tabela vazia (o caso da staging) o feed não deve bater na API — isso é o
    comportamento correto, não feed quebrado."""
    ep = SanidadeEndpoint()
    http = _Http({"inserted": 0, "updated": 0, "errors": []})
    res = ep.send(http, [])
    assert res.ok == [] and res.failed == [] and http.calls == []
