"""End-to-end pipeline test through orchestrator.run_cycle with fakes for Firebird
and HTTP — proves transform -> hash -> upsert -> select_unsent -> send -> mark_sent,
including change detection across cycles.
"""
import logging

from syncronizer.core import orchestrator
from syncronizer.core.endpoint import Endpoint
from syncronizer.core.extract import ExtractSpec
from syncronizer.db.store import ControlStore

LOG = logging.getLogger("test")


class FakeFB:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return self

    def fetch(self, sql, params=()):
        for row in self.rows:
            yield dict(row)

    def close(self):
        pass


class FakeHTTP:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if self.fail:
            raise RuntimeError("HTTP 500")


class Produtos(Endpoint):
    name = "produtos"
    primary_key = "id"
    api_path = "/p"

    def extract_spec(self, ctx):
        return ExtractSpec("SELECT ID, PRECO FROM PRODUTOS")

    def transform(self, row):
        return {"id": row["ID"], "preco": row["PRECO"]}


def test_full_cycle_change_detection_and_send(tmp_path):
    store = ControlStore(tmp_path / "c.db")
    ep = Produtos()

    # cycle 1: two new rows -> inserted + sent
    http = FakeHTTP()
    stats = orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}, {"ID": 2, "PRECO": 20}]),
                                   store, http, LOG, 500)
    est = stats.endpoint("produtos")
    assert (est.extracted, est.inserted, est.sent) == (2, 2, 2)
    assert len(http.calls) == 2
    assert store.select_unsent("produtos", 10) == []

    # cycle 2: identical -> all unchanged, nothing sent
    http = FakeHTTP()
    stats = orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}, {"ID": 2, "PRECO": 20}]),
                                   store, http, LOG, 500)
    est = stats.endpoint("produtos")
    assert (est.unchanged, est.changed, est.sent) == (2, 0, 0)
    assert http.calls == []

    # cycle 3: change row 2 -> only row 2 re-sent
    http = FakeHTTP()
    stats = orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}, {"ID": 2, "PRECO": 99}]),
                                   store, http, LOG, 500)
    est = stats.endpoint("produtos")
    assert (est.changed, est.sent) == (1, 1)
    assert len(http.calls) == 1
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/p"
    assert body == {"id": 2, "preco": 99}
    store.close()


def test_send_failure_keeps_row_unsent(tmp_path):
    store = ControlStore(tmp_path / "c.db")
    ep = Produtos()
    http = FakeHTTP(fail=True)
    stats = orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}]), store, http, LOG, 500)
    est = stats.endpoint("produtos")
    assert (est.failed, est.sent) == (1, 0)
    assert len(store.select_unsent("produtos", 10)) == 1
    attempts = store.conn.execute("SELECT send_attempts FROM ep_produtos WHERE pk='1'").fetchone()[0]
    assert attempts == 1
    store.close()


def test_reconcile_deletes_through_cycle(tmp_path):
    store = ControlStore(tmp_path / "c.db")

    class P2(Produtos):
        reconcile_deletes = True

    ep = P2()
    http = FakeHTTP()
    orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}, {"ID": 2, "PRECO": 20}]),
                           store, http, LOG, 500)
    # row 2 vanishes from source -> tombstoned + re-sent
    http = FakeHTTP()
    stats = orchestrator.run_cycle([ep], FakeFB([{"ID": 1, "PRECO": 10}]), store, http, LOG, 500)
    est = stats.endpoint("produtos")
    assert est.deleted == 1
    assert len(http.calls) == 1  # the tombstoned row is sent
    method, path, body = http.calls[0]
    assert method == "DELETE" and path == "/p/2" and body is None  # DELETE, not a re-POST
    store.close()
