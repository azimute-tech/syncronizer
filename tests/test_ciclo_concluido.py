"""Aviso de fim de ciclo (POST ciclo-concluido): payload, repetição persistida,
404 e ausência de chamada quando o Firebird está fora."""
import json
import logging

import pytest
import requests

from syncronizer import app as app_mod
from syncronizer.ciclo import API_PATH, META_KEY, CicloNotifier, build_payload, merge_payloads
from syncronizer.db.store import ControlStore
from syncronizer.runtime import CycleStats

LOG = logging.getLogger("test")


class Resp:
    def __init__(self, body=None, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        if self._body is None:
            raise ValueError("sem json")
        return self._body


def http_error(status):
    return requests.HTTPError(f"{status} Error", response=Resp(status=status))


class FakeHTTP:
    """Responde em sequência com o que estiver em ``script`` (exceção ou Resp)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        item = self.script.pop(0) if self.script else Resp({"versao": 1, "recalculado": True})
        if isinstance(item, Exception):
            raise item
        return item


def make_stats(fb=True):
    s = CycleStats(firebird_available=fb)
    a = s.endpoint("animais")
    a.sent, a.failed = 3, 1
    s.endpoint("lotes")  # sem mudança: 0/0 também vai no payload
    return s


@pytest.fixture
def store(tmp_path):
    st = ControlStore(tmp_path / "c.db")
    yield st
    st.close()


def notifier(store, http):
    sleeps = []
    n = CicloNotifier(store, http, LOG, sleep=sleeps.append)
    n.sleeps = sleeps
    return n


def test_payload_tem_todos_os_recursos_mesmo_sem_mudanca():
    p = build_payload(make_stats(), "2026-10-05T10:00:00+00:00", "2026-10-05T10:01:00+00:00",
                      ciclo_id="abc")
    assert p == {
        "ciclo_id": "abc",
        "iniciado_em": "2026-10-05T10:00:00+00:00",
        "concluido_em": "2026-10-05T10:01:00+00:00",
        "recursos": {"animais": {"enviados": 3, "falhas": 1},
                     "lotes": {"enviados": 0, "falhas": 0}},
    }


def test_sucesso_limpa_pendente_e_loga_versao(store, caplog):
    http = FakeHTTP(Resp({"versao": 7, "recalculado": True}))
    n = notifier(store, http)
    with caplog.at_level(logging.INFO, logger="test"):
        assert n.notify(build_payload(make_stats(), "a", "b", ciclo_id="c1")) is True
    assert http.calls[0][:2] == ("POST", API_PATH)
    assert n.pending() is None
    assert "versao=7 recalculado=True" in caplog.text


def test_falha_repete_com_espera_e_persiste(store):
    http = FakeHTTP(requests.ConnectionError("sem rede"), requests.exceptions.RetryError("5xx"),
                    http_error(503))
    n = notifier(store, http)
    assert n.notify(build_payload(make_stats(), "a", "b", ciclo_id="c1")) is False
    assert len(http.calls) == 3
    assert n.sleeps == [2.0, 5.0]
    # persistido no control.db: sobrevive a um novo notifier (reinício do serviço)
    assert json.loads(store.get_meta(META_KEY))["ciclo_id"] == "c1"
    n2 = notifier(store, FakeHTTP(Resp({"versao": 2, "recalculado": False})))
    assert n2.flush() is True
    assert n2.pending() is None


def test_pendente_se_funde_com_o_ciclo_novo(store):
    n = notifier(store, FakeHTTP(*[requests.ConnectionError("x")] * 3))
    n.notify(build_payload(make_stats(), "2026-10-05T10:00:00+00:00", "t1", ciclo_id="c1"))
    http = FakeHTTP(Resp({"versao": 3, "recalculado": True}))
    n.http = http
    assert n.notify(build_payload(make_stats(), "2026-10-05T10:15:00+00:00", "t2",
                                  ciclo_id="c2")) is True
    enviado = http.calls[0][2]
    assert len(http.calls) == 1  # uma chamada só para os dois ciclos
    assert enviado["ciclo_id"] == "c2"
    assert enviado["iniciado_em"] == "2026-10-05T10:00:00+00:00"
    assert enviado["concluido_em"] == "t2"
    assert enviado["recursos"]["animais"] == {"enviados": 6, "falhas": 2}


def test_merge_soma_recursos_distintos():
    m = merge_payloads({"ciclo_id": "a", "iniciado_em": "1", "recursos": {"x": {"enviados": 1, "falhas": 0}}},
                       {"ciclo_id": "b", "iniciado_em": "2", "concluido_em": "3",
                        "recursos": {"y": {"enviados": 2, "falhas": 1}}})
    assert m["recursos"] == {"x": {"enviados": 1, "falhas": 0}, "y": {"enviados": 2, "falhas": 1}}
    assert m["iniciado_em"] == "1"


def test_404_avisa_uma_vez_e_desliga_ate_reiniciar(store, caplog):
    http = FakeHTTP(http_error(404))
    n = notifier(store, http)
    with caplog.at_level(logging.WARNING, logger="test"):
        assert n.notify(build_payload(make_stats(), "a", "b")) is False
        assert n.notify(build_payload(make_stats(), "a", "b")) is False
        assert n.flush() is False
    assert len(http.calls) == 1
    assert n.sleeps == []
    assert n.pending() is None
    assert caplog.text.count("404") == 1
    # reinício (notifier novo) volta a tentar
    n2 = notifier(store, FakeHTTP())
    assert n2.notify(build_payload(make_stats(), "a", "b")) is True


def test_outro_4xx_descarta_sem_repetir(store):
    http = FakeHTTP(http_error(400))
    n = notifier(store, http)
    assert n.notify(build_payload(make_stats(), "a", "b")) is False
    assert len(http.calls) == 1 and n.pending() is None and not n.unsupported


def test_429_repete(store):
    http = FakeHTTP(http_error(429), Resp({"versao": 1, "recalculado": True}))
    n = notifier(store, http)
    assert n.notify(build_payload(make_stats(), "a", "b")) is True
    assert len(http.calls) == 2


# -- integração com Application.run_cycle ---------------------------------

class FakeFBClient:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


def make_app(store, http, monkeypatch, fb_ok):
    monkeypatch.setattr(app_mod, "FirebirdClient", FakeFBClient)
    monkeypatch.setattr(app_mod.orchestrator, "run_cycle",
                        lambda *a, **k: make_stats(fb=fb_ok))
    a = object.__new__(app_mod.Application)
    a.settings = type("S", (), {"batch_size": 500})()
    a.store, a.http, a.endpoints = store, http, []
    a.ciclo_notifier = CicloNotifier(store, http, LOG, sleep=lambda s: None)
    a._had_successful_cycle = False
    a.last_cycle_at = None
    a.last_fb_ok = False
    return a


def test_fim_do_ciclo_chama_ciclo_concluido(store, monkeypatch):
    http = FakeHTTP()
    a = make_app(store, http, monkeypatch, fb_ok=True)
    a.run_cycle()
    assert len(http.calls) == 1
    method, path, body = http.calls[0]
    assert (method, path) == ("POST", API_PATH)
    assert set(body) == {"ciclo_id", "iniciado_em", "concluido_em", "recursos"}
    assert body["concluido_em"] == a.last_cycle_at
    assert body["iniciado_em"] <= body["concluido_em"]
    assert body["recursos"]["animais"] == {"enviados": 3, "falhas": 1}


def test_sem_firebird_nao_chama(store, monkeypatch):
    http = FakeHTTP()
    a = make_app(store, http, monkeypatch, fb_ok=False)
    a.run_cycle()
    assert http.calls == []


def test_pendente_reenviado_no_inicio_do_ciclo_mesmo_sem_firebird(store, monkeypatch):
    CicloNotifier(store, FakeHTTP(*[requests.ConnectionError("x")] * 3), LOG,
                  sleep=lambda s: None).notify(build_payload(make_stats(), "a", "b", ciclo_id="velho"))
    http = FakeHTTP()
    a = make_app(store, http, monkeypatch, fb_ok=False)
    a.run_cycle()
    assert [c[2]["ciclo_id"] for c in http.calls] == ["velho"]
    assert a.ciclo_notifier.pending() is None


def test_erro_no_aviso_nao_derruba_o_ciclo(store, monkeypatch):
    http = FakeHTTP(*[requests.ConnectionError("x")] * 3)
    a = make_app(store, http, monkeypatch, fb_ok=True)
    stats = a.run_cycle()  # não levanta
    assert stats.firebird_available
    assert a.ciclo_notifier.pending() is not None


def test_timeout_nao_repete_dentro_do_ciclo(store):
    http = FakeHTTP(requests.ReadTimeout("lento"))
    n = notifier(store, http)
    assert n.notify(build_payload(make_stats(), "a", "b")) is False
    assert len(http.calls) == 1 and n.sleeps == []
    assert n.pending() is not None
