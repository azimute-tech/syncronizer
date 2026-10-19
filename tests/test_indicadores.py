"""Testes do orquestrador de indicadores (job), das peças comuns e do gating — tudo
mockado (sem rede real).

A fonte DATAGRO tem testes próprios em ``test_datagro.py``; aqui ``run_datagro``
é mockado para isolar o orquestrador (status, reaproveitamento, compatibilidade
com status antigo, lock).
"""
import json
import logging
from types import SimpleNamespace

import pytest

from syncronizer.indicadores import comum, datagro, job
from syncronizer.indicadores.comum import IndicadoresError

log = logging.getLogger("test.indicadores")

BLOCO_DATAGRO_OK = {
    "ok": True, "indicador": "DATAGRO_BOI_GORDO_SP", "fonte": "boletim",
    "datas": ["2026-08-05"], "valores_rs": [380.42], "data_indicador": "2026-08-05",
    "valor_rs": 380.42, "inserted": 0, "updated": 1, "quadro": "ok",
}


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


def _status_file(paths):
    return paths.indicadores_dir / "last_indicadores.json"


def _hoje(monkeypatch, dia="2026-08-06"):
    monkeypatch.setattr(job.timewin, "local_today", lambda s, now_utc=None: dia)


def _patch_datagro(monkeypatch, bloco=None, exc=None):
    """``run_datagro`` mockado; devolve a lista de chamadas (cada uma com ``hoje``)."""
    chamadas = []

    def fake(http, log, hoje=None):
        chamadas.append(hoje)
        if exc is not None:
            raise exc
        return dict(BLOCO_DATAGRO_OK if bloco is None else bloco)

    monkeypatch.setattr(datagro, "run_datagro", fake)
    return chamadas


# --------------------------------------------------------------------------- #
# comum: parse de R$ e conferência da resposta da API
# --------------------------------------------------------------------------- #
def test_parse_valor_brl():
    assert comum.parse_valor_brl("1.048,55") == pytest.approx(1048.55)
    assert comum.parse_valor_brl("348,55") == pytest.approx(348.55)


def test_conferir_resposta_desembrulha_data_e_devolve_registro():
    rec = comum.conferir_resposta({"data": {"inserted": 1, "updated": 4}}, 5, "DATAGRO", log)
    assert rec == {"inserted": 1, "updated": 4}


def test_conferir_resposta_200_com_errors_falha():
    with pytest.raises(IndicadoresError, match="rejeitou 1 de 5"):
        comum.conferir_resposta({"inserted": 0, "updated": 4, "errors": [
            {"index": 4, "chave": "DATAGRO_BOI_GORDO_SP", "message": "DATA no futuro"}]},
            5, "DATAGRO", log)


def test_conferir_resposta_confirmou_menos_falha():
    with pytest.raises(IndicadoresError, match="confirmou só 2 de 5"):
        comum.conferir_resposta({"inserted": 1, "updated": 1, "errors": []}, 5, "DATAGRO", log)


# --------------------------------------------------------------------------- #
# run_indicadores: status com o bloco datagro, ok global = datagro.ok
# --------------------------------------------------------------------------- #
def test_run_indicadores_happy_path(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    paths = _paths(tmp_path)

    status = job.run_indicadores(_settings(), paths, object(), log)

    assert status["ok"] is True
    assert status["datagro"] == BLOCO_DATAGRO_OK
    assert "error" not in status
    assert len(chamadas) == 1
    saved = json.loads(_status_file(paths).read_text())
    assert saved["ok"] is True
    assert set(saved) == {"ok", "data_referencia", "started_at", "finished_at", "datagro"}


def test_run_indicadores_usa_dia_local_como_referencia(tmp_path, monkeypatch):
    _patch_datagro(monkeypatch)
    _hoje(monkeypatch, "2026-08-06")
    status = job.run_indicadores(_settings(), _paths(tmp_path), object(), log)
    # data_referencia (gating do dia) é o dia LOCAL do envio; data_indicador é a da fonte
    assert status["data_referencia"] == "2026-08-06"
    assert status["datagro"]["data_indicador"] == "2026-08-05"


def test_run_indicadores_falha_da_fonte_nao_levanta(tmp_path, monkeypatch):
    _patch_datagro(monkeypatch, bloco={"ok": False, "indicador": "DATAGRO_BOI_GORDO_SP",
                                       "error": "DATAGRO boletim_cinco.svg respondeu status 503"})
    paths = _paths(tmp_path)
    s = _settings()

    status = job.run_indicadores(s, paths, object(), log)  # não levanta

    assert status["ok"] is False
    assert status["error"] == "datagro: DATAGRO boletim_cinco.svg respondeu status 503"
    assert json.loads(_status_file(paths).read_text())["ok"] is False
    assert job.indicadores_done_today(paths, s) is False   # catch-up vai retentar


def test_run_indicadores_excecao_inesperada_vira_status(tmp_path, monkeypatch):
    # run_datagro não levanta, mas se levantar o job segura e grava o erro
    _patch_datagro(monkeypatch, exc=RuntimeError("bug"))
    paths = _paths(tmp_path)
    status = job.run_indicadores(_settings(), paths, object(), log)
    assert status["ok"] is False
    assert status["datagro"] == {"ok": False, "error": "bug"}
    assert status["error"] == "datagro: bug"
    assert _status_file(paths).exists()


def test_run_indicadores_lock_ocupado_ignora_disparo(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    paths = _paths(tmp_path)
    assert job._LOCK.acquire(blocking=False)
    try:
        status = job.run_indicadores(_settings(), paths, object(), log)
    finally:
        job._LOCK.release()
    assert status == {"ok": False, "error": "envio já em andamento", "skipped": True}
    assert chamadas == [] and not _status_file(paths).exists()


# --------------------------------------------------------------------------- #
# reaproveitamento do bloco OK do dia (catch-up) x force (disparo manual)
# --------------------------------------------------------------------------- #
def test_run_indicadores_reaproveita_bloco_ok_do_dia(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    _status_file(paths).write_text(json.dumps({
        "ok": True, "data_referencia": "2026-08-06", "datagro": BLOCO_DATAGRO_OK}))

    status = job.run_indicadores(_settings(), paths, object(), log)

    assert status["ok"] is True
    assert status["datagro"] == dict(BLOCO_DATAGRO_OK, reaproveitado=True)
    assert chamadas == []


def test_run_indicadores_bloco_ok_de_ontem_nao_e_reaproveitado(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    _status_file(paths).write_text(json.dumps({
        "ok": True, "data_referencia": "2026-08-05", "datagro": BLOCO_DATAGRO_OK}))
    status = job.run_indicadores(_settings(), paths, object(), log)
    assert "reaproveitado" not in status["datagro"] and len(chamadas) == 1


def test_run_indicadores_force_refaz(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    _status_file(paths).write_text(json.dumps({
        "ok": True, "data_referencia": "2026-08-06", "datagro": BLOCO_DATAGRO_OK}))
    status = job.run_indicadores(_settings(), paths, object(), log, force=True)
    assert status["ok"] is True
    assert "reaproveitado" not in status["datagro"]
    assert len(chamadas) == 1


# --------------------------------------------------------------------------- #
# compatibilidade: status gravado pela versão anterior (com bloco "cepea")
# --------------------------------------------------------------------------- #
def test_status_antigo_com_cepea_falho_nao_faz_catchup_em_loop(tmp_path, monkeypatch):
    """Versão anterior gravava ok=False no topo quando o CEPEA falhava, mesmo com o
    DATAGRO OK. O bloco antigo é ignorado: o dia conta como feito e o catch-up para."""
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    s = _settings()
    _status_file(paths).write_text(json.dumps({
        "ok": False, "data_referencia": "2026-08-06",
        "error": "cepea: API rejeitou 1 de 1 item(ns)",
        "cepea": {"ok": False, "indicador": "CEPEA_BOI_GORDO", "error": "descontinuado"},
        "datagro": BLOCO_DATAGRO_OK,
    }))

    assert job.indicadores_done_today(paths, s) is True    # catch-up não dispara

    # e um disparo (cron) reaproveita o DATAGRO e regrava o status sem o bloco antigo
    status = job.run_indicadores(s, paths, object(), log)
    assert status["ok"] is True and status["datagro"]["reaproveitado"] is True
    assert chamadas == []
    saved = json.loads(_status_file(paths).read_text())
    assert set(saved) == {"ok", "data_referencia", "started_at", "finished_at", "datagro"}
    assert job.indicadores_done_today(paths, s) is True


def test_status_antigo_com_cepea_ok_e_datagro_falho_refaz_datagro(tmp_path, monkeypatch):
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    s = _settings()
    _status_file(paths).write_text(json.dumps({
        "ok": False, "data_referencia": "2026-08-06",
        "cepea": {"ok": True, "indicador": "CEPEA_BOI_GORDO"},
        "datagro": {"ok": False, "error": "503"},
    }))
    assert job.indicadores_done_today(paths, s) is False
    status = job.run_indicadores(s, paths, object(), log)
    assert status["ok"] is True and len(chamadas) == 1
    assert "cepea" not in json.loads(_status_file(paths).read_text())


def test_status_antigo_com_datagro_skipped_retoma_a_coleta(tmp_path, monkeypatch):
    """`datagro_enabled = false` (chave removida) gravava ok + skipped: não conta mais
    como feito, e o bloco não é reaproveitado — a coleta volta no mesmo dia."""
    chamadas = _patch_datagro(monkeypatch)
    _hoje(monkeypatch)
    paths = _paths(tmp_path)
    s = _settings()
    _status_file(paths).write_text(json.dumps({
        "ok": True, "data_referencia": "2026-08-06",
        "datagro": {"ok": True, "skipped": "desligado", "indicador": "DATAGRO_BOI_GORDO_SP"},
    }))
    assert job.indicadores_done_today(paths, s) is False
    status = job.run_indicadores(s, paths, object(), log)
    assert status["datagro"] == BLOCO_DATAGRO_OK and len(chamadas) == 1


# --------------------------------------------------------------------------- #
# indicadores_done_today: gating do catch-up (1 sucesso/dia por instância)
# --------------------------------------------------------------------------- #
def test_indicadores_done_today(tmp_path, monkeypatch):
    s = _settings()
    paths = _paths(tmp_path)
    _hoje(monkeypatch)
    status_file = _status_file(paths)

    # sem arquivo -> não feito
    assert job.indicadores_done_today(paths, s) is False
    # bloco datagro OK hoje -> feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-08-06",
                                       "datagro": {"ok": True}}))
    assert job.indicadores_done_today(paths, s) is True
    # status sem o bloco datagro (só "ok" no topo, versão bem antiga) -> não feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-08-06"}))
    assert job.indicadores_done_today(paths, s) is False
    # OK mas de ONTEM -> não feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-08-05",
                                       "datagro": {"ok": True}}))
    assert job.indicadores_done_today(paths, s) is False
    # de hoje mas FALHOU -> não feito
    status_file.write_text(json.dumps({"ok": False, "data_referencia": "2026-08-06",
                                       "datagro": {"ok": False}}))
    assert job.indicadores_done_today(paths, s) is False
    # arquivo ilegível -> não feito (nunca levanta)
    status_file.write_text("{quebrado")
    assert job.indicadores_done_today(paths, s) is False


# --------------------------------------------------------------------------- #
# parsing de [indicadores]
# --------------------------------------------------------------------------- #
def test_indicadores_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.indicadores_enabled is True    # default: ligado (todo instalador envia o DATAGRO)
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


def test_config_antigo_com_datagro_enabled_carrega(tmp_path, monkeypatch):
    """`datagro_enabled` saiu: config.toml antigo (agrupado ou gravado plano pelo painel)
    que ainda tem a chave carrega sem erro, e quem liga o job é `enabled`."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings

    (cfgdir / "config.toml").write_text(
        "config_version = 2\n[indicadores]\nenabled = true\ndatagro_enabled = false\n",
        encoding="utf-8",
    )
    s = load_settings()
    assert s.indicadores_enabled is True
    assert not hasattr(s, "indicadores_datagro_enabled")

    (cfgdir / "config.toml").write_text(
        "config_version = 2\nindicadores_enabled = false\nindicadores_datagro_enabled = true\n",
        encoding="utf-8",
    )
    s = load_settings()
    assert s.indicadores_enabled is False
    assert not hasattr(s, "indicadores_datagro_enabled")


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
    # catch-up também 1x logo após o boot (serviço relançado pelo self-update
    # depois das 20:30 coleta a fonte que falta sem esperar o interval de 30min)
    assert "indicadores_catchup_boot" in on.jobs
    assert on.funcs["indicadores_catchup_boot"] is on.funcs["indicadores_catchup"]
    off = _run_service(monkeypatch, indicadores_enabled=False)
    assert "indicadores" not in off.jobs and "indicadores_catchup" not in off.jobs
    assert "indicadores_catchup_boot" not in off.jobs


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
    assert _run_cli_once(tmp_path, monkeypatch, {"ok": True, "datagro": {"valor_rs": 380.42}}) == 0
    assert "380.42" in capsys.readouterr().out
    assert _run_cli_once(tmp_path, monkeypatch, {"ok": False, "error": "x"}) == 1
