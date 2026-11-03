"""Contrato de ``errors[]``: item recusado pela API nunca vira enviado.

Toda rota de ingestão TGC do AgroDB responde 200 com o mesmo corpo
(``TgcIngestResponseComAvisos``, montado em ``_lib/ingest.ts``)::

    {"inserted": 1, "updated": 0, "unchanged": 0, "avisos": [],
     "errors": [{"index": 1, "chave": "2", "message": "..."}]}

``chave`` é o identificador do item em TODAS as rotas. Um ``error_key`` diferente não
casa com nada: o 200 parece sucesso, a linha recusada é marcada como enviada e, com o
hash igual, nunca mais sai do control.db. Os testes rodam sobre o registro de feeds de
verdade, então um feed novo entra aqui sozinho.
"""
import json
import logging

import pytest
import requests

from syncronizer.core import orchestrator, registry
from syncronizer.db.store import ControlStore
from syncronizer.endpoints._common import BatchEndpoint
from syncronizer.runtime import EndpointStats

LOG = logging.getLogger("test")

FEEDS = [ep for ep in registry.discover() if isinstance(ep, BatchEndpoint)]


class _Receptor:
    """A API do lado de lá: recusa os itens pedidos e devolve o corpo real, por ``requests``."""

    def __init__(self, ep, recusar=()):
        self.ep = ep
        self.recusar = set(recusar)

    def request(self, method, path, json=None):
        itens = json[self.ep.payload_key]
        errors = [
            {"index": i, "chave": str(item[self.ep.record_key]), "message": "item recusado"}
            for i, item in enumerate(itens)
            if str(item[self.ep.record_key]) in self.recusar
        ]
        return _resposta({
            "inserted": len(itens) - len(errors), "updated": 0, "unchanged": 0,
            "errors": errors, "avisos": [],
        })


def _resposta(corpo):
    resp = requests.Response()
    resp.status_code = 200
    resp.headers["Content-Type"] = "application/json"
    resp._content = json.dumps(corpo).encode("utf-8")
    return resp


def _linha(ep, ident):
    return {"pk": ident, "payload": json.dumps({ep.record_key: ident}), "deleted": False}


def test_todos_os_feeds_foram_descobertos():
    # registry.discover() pula módulo que não importa; sem isto o contrato encolheria quieto
    assert len(FEEDS) >= 22


@pytest.mark.parametrize("ep", FEEDS, ids=lambda ep: ep.name)
def test_item_recusado_nao_vira_enviado(ep):
    res = ep.send(_Receptor(ep, recusar={"2"}), [_linha(ep, "1"), _linha(ep, "2")])
    assert res.ok == ["1"]
    assert res.failed == [("2", "api: item recusado")]


@pytest.mark.parametrize("ep", FEEDS, ids=lambda ep: ep.name)
def test_item_recusado_fica_pendente_e_volta_no_ciclo_seguinte(tmp_path, ep):
    store = ControlStore(tmp_path / "c.db")
    store.ensure_endpoint_table(ep.name)
    store.upsert_many(ep.name, [
        (ident, _linha(ep, ident)["payload"], f"hash-{ident}") for ident in ("1", "2")
    ])

    est = EndpointStats(name=ep.name)
    orchestrator._send_endpoint(ep, store, _Receptor(ep, recusar={"2"}), LOG, est, 500)
    assert (est.sent, est.failed) == (1, 1)
    assert store.endpoint_counts(ep.name)["pending"] == 1
    assert [r["pk"] for r in store.select_unsent(ep.name, 10)] == ["2"]

    # corrigido na origem (ou aceito pela API): a mesma linha sai sem re-extrair
    orchestrator._send_endpoint(ep, store, _Receptor(ep), LOG, EndpointStats(name=ep.name), 500)
    assert store.endpoint_counts(ep.name)["pending"] == 0
    store.close()
