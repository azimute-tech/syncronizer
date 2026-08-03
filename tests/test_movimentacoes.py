"""Tests for the `movimentacoes` feed — the first endpoint that actually uses the
incremental watermark machinery, so the watermark is exercised end-to-end here (not
only through orchestrator._watermark_max, which test_watermark.py covers in isolation).
"""
import json
import logging
from datetime import date

from syncronizer.core import orchestrator
from syncronizer.core.extract import ExtractContext
from syncronizer.db.store import ControlStore
from syncronizer.endpoints.movimentacoes import MovimentacoesEndpoint

LOG = logging.getLogger("test")


def _row(**over):
    base = {
        "AHT_CODIGO": 652, "COD_ANIMAL": 1, "DATA_EVENTO": date(2026, 1, 28),
        "TIPO_ATMF": 3, "TIPO_STATUS": "ENTRADA", "TIPO_DESCRICAO": "COMPRA",
        "LOTE_ORIGEM": 10001, "LOTE_DESTINO": 10001,
        "CURRAL_ORIGEM": "P1-1", "CURRAL_DESTINO": "P1-1",
        "MOTIVO": "ENTRADA_CONFINAMENTO",
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = MovimentacoesEndpoint()
    r = ep.transform(_row())
    assert r["COD_HISTORICO"] == "652" and ep.make_pk(r) == "652"
    assert r["COD_ANIMAL"] == "1"
    assert r["DATA_EVENTO"] == "2026-01-28"
    assert r["TIPO_ATMF"] == 3 and isinstance(r["TIPO_ATMF"], int)
    assert r["TIPO_STATUS"] == "ENTRADA" and r["TIPO_DESCRICAO"] == "COMPRA"
    assert r["LOTE_ORIGEM"] == "10001" and r["LOTE_DESTINO"] == "10001"
    assert r["CURRAL_ORIGEM"] == "P1-1" and r["CURRAL_DESTINO"] == "P1-1"
    assert r["MOTIVO"] == "ENTRADA_CONFINAMENTO"


def test_curral_columns_carry_the_name_not_the_code():
    """AHT_CURRALORIGEM/DESTINO are VARCHAR(10) holding CC_NOME — that is how TGC writes
    this table, and AgroDB has curral_origem_nome/curral_destino_nome for exactly it.

    Deliberate contrast with animais.CURRAL_ATUAL, which carries the CODE. Do not
    "unify" the two: converting names to codes here would invent data, and the name is
    all TGC recorded at the time of the event."""
    ep = MovimentacoesEndpoint()
    r = ep.transform(_row(CURRAL_ORIGEM="P35-4  ", CURRAL_DESTINO="ENF-1"))
    assert r["CURRAL_ORIGEM"] == "P35-4"   # padding stripped
    assert r["CURRAL_DESTINO"] == "ENF-1"
    # no join to CAD_CURRAL: the raw VARCHAR travels as-is
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_CURRAL" not in spec.sql


def test_unknown_movement_type_still_travels():
    """The AUX_TIPO_MOV_FISICA join is a LEFT JOIN: an id with no matching row must not
    make the movement disappear — the destination has to see the gap."""
    r = MovimentacoesEndpoint().transform(
        _row(TIPO_ATMF=99, TIPO_STATUS=None, TIPO_DESCRICAO=None)
    )
    assert r["TIPO_ATMF"] == 99
    assert r["TIPO_STATUS"] is None and r["TIPO_DESCRICAO"] is None


def test_blank_motivo_becomes_null():
    assert MovimentacoesEndpoint().transform(_row(MOTIVO="   "))["MOTIVO"] is None


def test_extract_spec_first_run_reads_everything():
    ep = MovimentacoesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_HISTORICOTRASNF" in spec.sql and "AUX_TIPO_MOV_FISICA" in spec.sql
    assert spec.params == () and spec.incremental is False
    assert "WHERE" not in spec.sql.upper()
    assert "LIMIT" not in spec.sql.upper()          # Firebird uses FIRST/SKIP


def test_extract_spec_incremental_uses_qmark_and_numeric_param():
    """The watermark round-trips through a TEXT column; it must be bound back as the
    integer the column really is, with a qmark placeholder (firebirdsql paramstyle)."""
    ep = MovimentacoesEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark="3950"))
    assert spec.incremental is True
    assert "WHERE h.AHT_CODIGO > ?" in spec.sql
    assert ":" not in spec.sql.split("SELECT", 1)[1]  # no :named binds
    assert spec.params == (3950,)
    # the watermark column is selected UN-ALIASED, otherwise the orchestrator's
    # row.get("AHT_CODIGO") returns None and the watermark never advances
    assert ep.incremental_column == "AHT_CODIGO"
    assert "h.AHT_CODIGO,\n" in spec.sql


def test_extract_spec_tolerates_non_numeric_watermark():
    spec = MovimentacoesEndpoint().extract_spec(ExtractContext(last_watermark="abc"))
    assert spec.params == ("abc",)


def test_incremental_never_reconciles_deletes():
    """Tombstoning is only valid after a full scan; from a delta it would wipe the
    whole table."""
    assert MovimentacoesEndpoint().reconcile_deletes is False


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
    r = MovimentacoesEndpoint().transform(_row(AHT_CODIGO=cod))
    return {"pk": str(cod), "payload": json.dumps(r), "deleted": False}


def test_send_posts_batch_under_movimentacoes_key_without_farm_id():
    ep = MovimentacoesEndpoint()
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(652), _unsent(653)])
    assert set(res.ok) == {"652", "653"} and res.failed == []
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/movimentacoes"
    assert len(body["movimentacoes"]) == 2
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["movimentacoes"])


def test_send_reconciles_per_item_errors():
    ep = MovimentacoesEndpoint()
    http = _Http({"inserted": 1,
                  "errors": [{"cod_historico": "653", "message": "animal inexistente"}]})
    res = ep.send(http, [_unsent(652), _unsent(653)])
    assert res.ok == ["652"] and [pk for pk, _ in res.failed] == ["653"]


# --- watermark end-to-end -------------------------------------------------


class _FakeFB:
    """Firebird stand-in that honours the incremental WHERE clause, so the watermark is
    tested against the query the endpoint actually builds."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def connect(self):
        return self

    def fetch(self, sql, params=()):
        self.queries.append((sql, tuple(params)))
        after = params[0] if params else None
        for row in self.rows:
            if after is None or row["AHT_CODIGO"] > int(after):
                yield dict(row)

    def close(self):
        pass


class _CycleHTTP:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return _Resp({"inserted": len(json["movimentacoes"]), "errors": []})


def test_watermark_advances_and_scopes_the_next_extract(tmp_path):
    store = ControlStore(tmp_path / "c.db")
    ep = MovimentacoesEndpoint()

    # cycle 1: no watermark -> full history
    fb = _FakeFB([_row(AHT_CODIGO=652), _row(AHT_CODIGO=653, COD_ANIMAL=2)])
    http = _CycleHTTP()
    stats = orchestrator.run_cycle([ep], fb, store, http, LOG, 500)
    est = stats.endpoint("movimentacoes")
    assert (est.extracted, est.inserted, est.sent) == (2, 2, 2)
    assert fb.queries[0][1] == ()                      # first extract has no bind
    assert store.get_watermark("movimentacoes") == "653"

    # cycle 2: only the new event is extracted; already-synced rows are not re-read
    fb = _FakeFB([_row(AHT_CODIGO=652), _row(AHT_CODIGO=653, COD_ANIMAL=2),
                  _row(AHT_CODIGO=654, COD_ANIMAL=3)])
    http = _CycleHTTP()
    stats = orchestrator.run_cycle([ep], fb, store, http, LOG, 500)
    est = stats.endpoint("movimentacoes")
    assert (est.extracted, est.inserted, est.sent) == (1, 1, 1)
    assert fb.queries[0][1] == (653,)                  # bound as int, not '653'
    assert "WHERE h.AHT_CODIGO > ?" in fb.queries[0][0]
    assert store.get_watermark("movimentacoes") == "654"
    assert http.calls[0][2]["movimentacoes"][0]["COD_HISTORICO"] == "654"

    # cycle 3: nothing new -> no extract rows, no POST, watermark held
    fb = _FakeFB([_row(AHT_CODIGO=652), _row(AHT_CODIGO=653), _row(AHT_CODIGO=654)])
    http = _CycleHTTP()
    stats = orchestrator.run_cycle([ep], fb, store, http, LOG, 500)
    assert stats.endpoint("movimentacoes").extracted == 0
    assert http.calls == []
    assert store.get_watermark("movimentacoes") == "654"
    store.close()


def test_watermark_does_not_regress_past_nine(tmp_path):
    """A TEXT watermark compared lexicographically would freeze at '9' ('9' > '10');
    the whole feed would stop after the ninth event."""
    store = ControlStore(tmp_path / "c.db")
    ep = MovimentacoesEndpoint()
    fb = _FakeFB([_row(AHT_CODIGO=i) for i in range(1, 11)])
    orchestrator.run_cycle([ep], fb, store, _CycleHTTP(), LOG, 500)
    assert store.get_watermark("movimentacoes") == "10"
    store.close()
