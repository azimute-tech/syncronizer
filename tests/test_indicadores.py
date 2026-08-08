"""Testes do job de indicadores (CEPEA boi gordo) — tudo mockado (sem rede real).

A fixture ``tests/fixtures/cepea_widgetproduto.js`` é a resposta REAL do widget
``widgetproduto.js.php?id_indicador[]=2`` (capturada em 06/08/2026): um
``document.write(...)`` com a tabela do Indicador do Boi Gordo CEPEA/B3
(05/08/2026, R$ 348,55/@).
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncronizer.indicadores import cepea
from syncronizer.indicadores.cepea import IndicadoresError

log = logging.getLogger("test.indicadores")

FIXTURE = Path(__file__).parent / "fixtures" / "cepea_widgetproduto.js"


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _settings(**over):
    base = dict(
        api_base_url="https://api.example.com",
        api_key="k",
        api_token="",
        indicadores_enabled=True,
        indicadores_hour=20,
        indicadores_minute=30,
        tz_offset_hours=-3,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _paths(tmp_path):
    ind_dir = tmp_path / "state" / "indicadores"
    ind_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(indicadores_dir=ind_dir)


class _Resp:
    def __init__(self, data, content=b"{}", status=200, text=""):
        self._data = data
        self.content = content
        self.status_code = status
        self.text = text

    def json(self):
        return self._data


class _Http:
    """Fake do app.http: registra as chamadas (method, path, json)."""

    def __init__(self, post_resp=None, raise_exc=None):
        self.post_resp = post_resp if post_resp is not None else {"data": {"inserted": 1, "updated": 0}}
        self.raise_exc = raise_exc
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(self.post_resp)


def _patch_widget(monkeypatch, text=None, exc=None, status=200):
    """Patch do requests.get do módulo cepea; devolve o dict de captura."""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        if exc is not None:
            raise exc
        body = text if text is not None else FIXTURE.read_text(encoding="utf-8")
        return _Resp({}, status=status, text=body)

    monkeypatch.setattr(cepea.requests, "get", fake_get)
    return captured


def _widget_html(data="05/08/2026", valor="348,55"):
    """Widget sintético com a mesma estrutura de células do real."""
    return f"""
    <table class="imagenet-widget-tabela">
      <thead><tr><th>Data</th><th>Produto</th><th>Valor</th></tr></thead>
      <tfoot><tr><td colspan="2">Fonte: Cepea</td><td></td></tr></tfoot>
      <tbody><tr>
        <td>{data}</td>
        <td><span class="maior">Boi Gordo</span><br /> <span class="unidade">@</span></td>
        <td>R$ <span class="maior">{valor}</span></td>
      </tr></tbody>
    </table>
    """


# --------------------------------------------------------------------------- #
# parser: fixture REAL do widget
# --------------------------------------------------------------------------- #
def test_parse_widget_fixture_real():
    parsed = cepea.parse_widget(FIXTURE.read_text(encoding="utf-8"))
    assert parsed == {"data": "2026-08-05", "valor_rs": 348.55}


def test_parse_widget_converte_data_e_virgula():
    parsed = cepea.parse_widget(_widget_html(data="31/12/2026", valor="512,03"))
    assert parsed["data"] == "2026-12-31"
    assert parsed["valor_rs"] == pytest.approx(512.03)


def test_parse_widget_valor_com_milhar():
    # "R$ 1.000,00" (ponto de milhar) tem que virar 1000.0 — teto da faixa, aceito.
    parsed = cepea.parse_widget(_widget_html(valor="1.000,00"))
    assert parsed["valor_rs"] == pytest.approx(1000.0)


def test_parse_widget_ignora_headers():
    # Os headers "Data"/"Produto"/"Valor"/"Fonte: Cepea" não podem casar como célula
    # de dado: o resultado vem da linha do tbody.
    parsed = cepea.parse_widget(_widget_html())
    assert parsed == {"data": "2026-08-05", "valor_rs": 348.55}


# --------------------------------------------------------------------------- #
# validação de faixa (100–1000 R$/@): fora = página mudou -> erro, não envia lixo
# --------------------------------------------------------------------------- #
def test_parse_widget_valor_abaixo_da_faixa_raises():
    with pytest.raises(IndicadoresError, match="faixa"):
        cepea.parse_widget(_widget_html(valor="99,99"))


def test_parse_widget_valor_acima_da_faixa_raises():
    with pytest.raises(IndicadoresError, match="faixa"):
        cepea.parse_widget(_widget_html(valor="1.000,01"))


def test_parse_widget_limites_da_faixa_aceitos():
    assert cepea.parse_widget(_widget_html(valor="100,00"))["valor_rs"] == 100.0
    assert cepea.parse_widget(_widget_html(valor="1.000,00"))["valor_rs"] == 1000.0


def test_parse_widget_sem_celulas_raises():
    with pytest.raises(IndicadoresError, match="formato mudou"):
        cepea.parse_widget("<html>mudou tudo</html>")


def test_parse_widget_data_invalida_raises():
    with pytest.raises(IndicadoresError, match="data inválida"):
        cepea.parse_widget(_widget_html(data="99/99/2026"))


# --------------------------------------------------------------------------- #
# fetch: User-Agent de browser, sessão nova (sem auth do AgroDB), non-2xx -> erro
# --------------------------------------------------------------------------- #
def test_fetch_widget_user_agent_de_browser_sem_auth(monkeypatch):
    captured = _patch_widget(monkeypatch)
    html = cepea.fetch_widget(log)
    assert "document.write" in html
    assert captured["url"] == cepea.CEPEA_WIDGET_URL
    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]
    # sessão NOVA: nenhum header de auth do AgroDB vaza pro CEPEA
    assert "X-API-Key" not in captured["headers"]
    assert "Authorization" not in captured["headers"]


def test_fetch_widget_non_2xx_raises(monkeypatch):
    _patch_widget(monkeypatch, status=403)
    with pytest.raises(IndicadoresError, match="403"):
        cepea.fetch_widget(log)


# --------------------------------------------------------------------------- #
# fluxo feliz: payload exato, nada específico da fazenda, status persistido
# --------------------------------------------------------------------------- #
def test_run_indicadores_happy_path(tmp_path, monkeypatch):
    _patch_widget(monkeypatch)
    s = _settings()
    paths = _paths(tmp_path)
    http = _Http(post_resp={"data": {"inserted": 1, "updated": 0}})

    status = cepea.run_indicadores(s, paths, http, log)

    assert status["ok"] is True
    assert status["indicador"] == "CEPEA_BOI_GORDO"
    assert status["data_indicador"] == "2026-08-05"
    assert status["valor_rs"] == 348.55
    # exatamente um POST na rota de indicadores, com o payload do contrato
    assert [(c[0], c[1]) for c in http.calls] == [("POST", "/api/integracoes/indicadores")]
    body = http.calls[0][2]
    assert body == {"indicadores": [
        {"INDICADOR": "CEPEA_BOI_GORDO", "DATA": "2026-08-05", "VALOR_RS": 348.55},
    ]}
    # indicador é NACIONAL: payload sem nada específico da fazenda (o token identifica)
    assert set(body["indicadores"][0].keys()) == {"INDICADOR", "DATA", "VALOR_RS"}
    # status persistido
    saved = json.loads((paths.indicadores_dir / "last_indicadores.json").read_text())
    assert saved["ok"] is True


def test_run_indicadores_usa_dia_local_como_referencia(tmp_path, monkeypatch):
    _patch_widget(monkeypatch)
    monkeypatch.setattr(cepea.timewin, "local_today", lambda s, now_utc=None: "2026-08-06")
    status = cepea.run_indicadores(_settings(), _paths(tmp_path), _Http(), log)
    # data_referencia (gating do dia) é o dia LOCAL do envio; data_indicador é a do CEPEA
    assert status["data_referencia"] == "2026-08-06"
    assert status["data_indicador"] == "2026-08-05"


# --------------------------------------------------------------------------- #
# idempotência: N fazendas enviam o mesmo dia -> API responde updated, e isso é
# SUCESSO normal (conta como o sucesso do dia da instância)
# --------------------------------------------------------------------------- #
def test_run_indicadores_upsert_updated_e_sucesso_do_dia(tmp_path, monkeypatch):
    _patch_widget(monkeypatch)
    monkeypatch.setattr(cepea.timewin, "local_today", lambda s, now_utc=None: "2026-08-06")
    s = _settings()
    paths = _paths(tmp_path)
    # outra fazenda já tinha enviado: a API faz upsert e responde updated>0/inserted=0
    http = _Http(post_resp={"data": {"inserted": 0, "updated": 1}})

    status = cepea.run_indicadores(s, paths, http, log)

    assert status["ok"] is True            # re-envio NÃO é erro nem warning
    assert status["inserted"] == 0
    assert status["updated"] == 1
    # e conta como o sucesso do dia: o catch-up não dispara de novo
    assert cepea.indicadores_done_today(paths, s) is True


# --------------------------------------------------------------------------- #
# falhas: nunca levanta, não envia lixo, status ok=False (catch-up retenta)
# --------------------------------------------------------------------------- #
def test_run_indicadores_fetch_falha_nao_levanta(tmp_path, monkeypatch):
    import requests as _requests
    _patch_widget(monkeypatch, exc=_requests.ConnectionError("internet da fazenda caiu"))
    http = _Http()
    paths = _paths(tmp_path)

    status = cepea.run_indicadores(_settings(), paths, http, log)  # não levanta

    assert status["ok"] is False
    assert "error" in status
    assert http.calls == []               # nada foi enviado à API
    saved = json.loads((paths.indicadores_dir / "last_indicadores.json").read_text())
    assert saved["ok"] is False


def test_run_indicadores_valor_fora_da_faixa_nao_envia(tmp_path, monkeypatch):
    _patch_widget(monkeypatch, text=_widget_html(valor="9.999,99"))
    http = _Http()

    status = cepea.run_indicadores(_settings(), _paths(tmp_path), http, log)

    assert status["ok"] is False
    assert "faixa" in status["error"]
    assert http.calls == []               # lixo NUNCA chega na API


def test_run_indicadores_post_falha_nao_levanta(tmp_path, monkeypatch):
    _patch_widget(monkeypatch)
    http = _Http(raise_exc=RuntimeError("500 Server Error"))
    paths = _paths(tmp_path)
    s = _settings()

    status = cepea.run_indicadores(s, paths, http, log)

    assert status["ok"] is False
    assert cepea.indicadores_done_today(paths, s) is False  # catch-up vai retentar


# --------------------------------------------------------------------------- #
# indicadores_done_today: gating do catch-up (1 sucesso/dia por instância)
# --------------------------------------------------------------------------- #
def test_indicadores_done_today(tmp_path, monkeypatch):
    s = _settings()
    paths = _paths(tmp_path)
    monkeypatch.setattr(cepea.timewin, "local_today", lambda s, now_utc=None: "2026-08-06")
    status_file = paths.indicadores_dir / "last_indicadores.json"

    # sem arquivo -> não feito
    assert cepea.indicadores_done_today(paths, s) is False
    # OK no dia de hoje -> feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-08-06"}))
    assert cepea.indicadores_done_today(paths, s) is True
    # OK mas de ONTEM -> não feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-08-05"}))
    assert cepea.indicadores_done_today(paths, s) is False
    # de hoje mas FALHOU -> não feito
    status_file.write_text(json.dumps({"ok": False, "data_referencia": "2026-08-06"}))
    assert cepea.indicadores_done_today(paths, s) is False


# --------------------------------------------------------------------------- #
# parsing de [indicadores]
# --------------------------------------------------------------------------- #
def test_indicadores_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.indicadores_enabled is False   # default: desligado
    assert s.indicadores_hour == 20
    assert s.indicadores_minute == 30


def test_indicadores_section_flattens(tmp_path, monkeypatch):
    """[indicadores] enabled = true deve resolver indicadores_enabled."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.toml").write_text(
        "[indicadores]\nenabled = true\nhour = 21\nminute = 15\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.indicadores_enabled is True
    assert s.indicadores_hour == 21 and s.indicadores_minute == 15


# --------------------------------------------------------------------------- #
# conversão local -> UTC do cron
# --------------------------------------------------------------------------- #
def test_local_hm_to_utc():
    from syncronizer import timewin
    s = SimpleNamespace(tz_offset_hours=-3)
    assert timewin.local_hm_to_utc(s, 20, 30) == (23, 30)   # 20:30 BRT = 23:30 UTC
    s = SimpleNamespace(tz_offset_hours=-5)
    assert timewin.local_hm_to_utc(s, 22, 0) == (3, 0)      # wrap de meia-noite


# --------------------------------------------------------------------------- #
# gating do scheduler (mesmo molde do backup)
# --------------------------------------------------------------------------- #
class _Sched:
    def __init__(self, *a, **k):
        self.jobs = []
        self.funcs = {}

    def add_job(self, func, trigger, **kw):
        self.jobs.append(kw.get("id"))
        self.funcs[kw.get("id")] = func

    def start(self):
        raise SystemExit  # corta o loop imediatamente

    def shutdown(self, *a, **k):
        pass


def _run_service(monkeypatch, **over):
    import syncronizer.scheduler as scheduler

    created = {}
    monkeypatch.setattr(scheduler, "BlockingScheduler",
                        lambda *a, **k: created.setdefault("sched", _Sched()))
    monkeypatch.setattr(scheduler.webserver, "start", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a, **k: None)

    base = dict(
        misfire_grace_time=300, boot_grace_minutes=5, cycle_minutes=10,
        update_minutes=30, auto_update=False, run_on_start=False,
        backup_enabled=False, backup_hour=20, backup_minute=0, backup_compression="gzip",
        indicadores_enabled=False, indicadores_hour=20, indicadores_minute=30,
        tz_offset_hours=-3, etl_window_enabled=True,
        etl_window_start_hour=7, etl_window_end_hour=19,
    )
    base.update(over)
    s = SimpleNamespace(**base)
    app = SimpleNamespace(
        settings=s, had_successful_cycle=False, restart_requested=False,
        run_cycle=lambda: None, close=lambda: None,
        paths=SimpleNamespace(), http=None,
    )
    try:
        scheduler.run_service(app, log)
    except SystemExit:
        pass
    return created["sched"]


def test_scheduler_adds_indicadores_jobs_only_when_enabled(monkeypatch):
    on = _run_service(monkeypatch, indicadores_enabled=True)
    assert "indicadores" in on.jobs and "indicadores_catchup" in on.jobs
    off = _run_service(monkeypatch, indicadores_enabled=False)
    assert "indicadores" not in off.jobs and "indicadores_catchup" not in off.jobs


def test_scheduler_indicadores_catchup_gating(monkeypatch):
    """Catch-up só roda DEPOIS do horário e se o envio de hoje ainda não foi OK."""
    import syncronizer.indicadores as ind_pkg
    import syncronizer.scheduler as scheduler
    from datetime import datetime

    sched = _run_service(monkeypatch, indicadores_enabled=True)
    catchup = sched.funcs["indicadores_catchup"]

    runs = {"n": 0}
    monkeypatch.setattr(ind_pkg, "run_indicadores",
                        lambda *a, **k: runs.__setitem__("n", runs["n"] + 1))

    # 1) antes das 20:30 locais: não dispara
    monkeypatch.setattr(scheduler.timewin, "local_now",
                        lambda s, now_utc=None: datetime(2026, 8, 6, 19, 0))
    monkeypatch.setattr(ind_pkg, "indicadores_done_today", lambda paths, s: False)
    catchup()
    assert runs["n"] == 0

    # 2) depois das 20:30 e ainda não concluído hoje: dispara
    monkeypatch.setattr(scheduler.timewin, "local_now",
                        lambda s, now_utc=None: datetime(2026, 8, 6, 21, 0))
    catchup()
    assert runs["n"] == 1

    # 3) depois das 20:30 mas já concluído hoje: não dispara de novo (1 sucesso/dia)
    monkeypatch.setattr(ind_pkg, "indicadores_done_today", lambda paths, s: True)
    catchup()
    assert runs["n"] == 1


# --------------------------------------------------------------------------- #
# CLI: indicadores-once (validação do staging) — exit code espelha o status
# --------------------------------------------------------------------------- #
def _run_cli_once(tmp_path, monkeypatch, status):
    import syncronizer.indicadores as ind_pkg
    from syncronizer.__main__ import main
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ind_pkg, "run_indicadores", lambda *a, **k: status)
    return main(["indicadores-once"])


def test_cli_indicadores_once_exit_codes(tmp_path, monkeypatch, capsys):
    assert _run_cli_once(tmp_path, monkeypatch, {"ok": True, "valor_rs": 348.55}) == 0
    assert "348.55" in capsys.readouterr().out
    assert _run_cli_once(tmp_path, monkeypatch, {"ok": False, "error": "x"}) == 1
