"""Testes do backup do banco para o GCS — tudo mockado (sem Firebird/rede real)."""
import gzip
import hashlib
import json
import logging
import lzma
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncronizer.backup import gcs_backup
from syncronizer.backup.gcs_backup import BackupError

log = logging.getLogger("test.backup")


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _settings(tmp_path, **over):
    base = dict(
        firebird_path=tmp_path / "app.fdb",
        firebird_host="localhost",
        firebird_port=3050,
        firebird_user="SYSDBA",
        firebird_password="masterkey",
        api_base_url="https://api.example.com",
        api_key="k",
        api_token="",
        backup_enabled=True,
        backup_gbak_path=tmp_path / "gbak",
        backup_gbak_use_service=True,
        backup_db_alias="agrodb",
        backup_compression="gzip",
        backup_temp_dir=tmp_path / "temp",
        backup_upload_read_timeout=600,
        backup_max_retries=3,
        backup_min_free_disk_multiplier=2.5,
        backup_hour=20,
        backup_minute=0,
        tz_offset_hours=-3,
    )
    base.update(over)
    s = SimpleNamespace(**base)
    s.require_firebird_path = lambda: Path(s.firebird_path)
    return s


def _paths(tmp_path):
    backup_dir = tmp_path / "state" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(backup_dir=backup_dir)


class _Resp:
    def __init__(self, data, content=b"{}", status=200):
        self._data = data
        self.content = content
        self.status_code = status

    def json(self):
        return self._data


class _Http:
    """Fake do app.http: registra as chamadas (method, path, json)."""

    def __init__(self, upload_resp, confirm_resp):
        self.upload_resp = upload_resp
        self.confirm_resp = confirm_resp
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if path.endswith("/upload-url"):
            return _Resp(self.upload_resp)
        if path.endswith("/confirm"):
            return _Resp(self.confirm_resp)
        raise AssertionError(f"path inesperado: {path}")


# --------------------------------------------------------------------------- #
# compress: roundtrip + sha256 (gzip e xz)
# --------------------------------------------------------------------------- #
def test_compress_gzip_roundtrip_and_sha256(tmp_path):
    src = tmp_path / "in.fbk"
    payload = b"conteudo do backup " * 5000
    src.write_bytes(payload)
    dst = tmp_path / "out.fbk.gz"
    s = _settings(tmp_path, backup_compression="gzip")

    out, size, sha = gcs_backup.compress(src, dst, s, log)

    assert out == dst and dst.is_file()
    assert gzip.open(dst, "rb").read() == payload          # roundtrip
    assert size == dst.stat().st_size                       # tamanho do comprimido
    assert sha == hashlib.sha256(dst.read_bytes()).hexdigest()  # sha do COMPRIMIDO


def test_compress_xz_roundtrip_and_sha256(tmp_path):
    src = tmp_path / "in.fbk"
    payload = b"dados " * 10000
    src.write_bytes(payload)
    dst = tmp_path / "out.fbk.xz"
    s = _settings(tmp_path, backup_compression="xz")

    out, size, sha = gcs_backup.compress(src, dst, s, log)

    assert lzma.open(dst, "rb").read() == payload
    assert size == dst.stat().st_size
    assert sha == hashlib.sha256(dst.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# gbak: ok / erro(nonzero) / timeout / ausente (e NÃO copia o .fdb)
# --------------------------------------------------------------------------- #
def _make_fdb(tmp_path, size=4096):
    fdb = tmp_path / "app.fdb"
    fdb.write_bytes(b"\x00" * size)
    return fdb


def test_produce_backup_ok(tmp_path, monkeypatch):
    _make_fdb(tmp_path)
    (tmp_path / "gbak").write_text("#!/bin/sh\n")  # gbak "presente"
    s = _settings(tmp_path)
    paths = _paths(tmp_path)

    def fake_run(argv, **kw):
        # o gbak gera o .fbk no path de destino (último arg)
        Path(argv[-1]).write_bytes(b"FBKDATA" * 100)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gcs_backup.subprocess, "run", fake_run)
    fbk = gcs_backup.produce_backup(s, paths, log)
    assert fbk.is_file() and fbk.suffix == ".fbk"
    assert fbk.stat().st_size > 0


def test_produce_backup_nonzero_raises(tmp_path, monkeypatch):
    _make_fdb(tmp_path)
    (tmp_path / "gbak").write_text("x")
    s = _settings(tmp_path)
    paths = _paths(tmp_path)

    monkeypatch.setattr(
        gcs_backup.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(BackupError, match="gbak falhou"):
        gcs_backup.produce_backup(s, paths, log)


def test_produce_backup_timeout_raises(tmp_path, monkeypatch):
    _make_fdb(tmp_path)
    (tmp_path / "gbak").write_text("x")
    s = _settings(tmp_path)
    paths = _paths(tmp_path)

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 1))

    monkeypatch.setattr(gcs_backup.subprocess, "run", boom)
    with pytest.raises(BackupError, match="timeout"):
        gcs_backup.produce_backup(s, paths, log)


def test_produce_backup_gbak_missing_raises_and_no_raw_copy(tmp_path, monkeypatch):
    fdb = _make_fdb(tmp_path)
    # gbak ausente: backup_gbak_path aponta pra arquivo que não existe
    s = _settings(tmp_path, backup_gbak_path=tmp_path / "naoexiste-gbak")
    paths = _paths(tmp_path)

    called = {"run": False}
    monkeypatch.setattr(
        gcs_backup.subprocess, "run",
        lambda *a, **k: called.__setitem__("run", True),
    )
    with pytest.raises(BackupError, match="gbak não encontrado"):
        gcs_backup.produce_backup(s, paths, log)
    assert called["run"] is False
    # NÃO copia o .fdb cru: nenhum .fbk surge no temp
    temp = gcs_backup.resolve_backup_temp(s, paths)
    assert list(temp.glob("*.fbk")) == []
    # e o .fdb original permanece intocado no lugar
    assert fdb.is_file()


# --------------------------------------------------------------------------- #
# pré-check de disco aborta
# --------------------------------------------------------------------------- #
def test_disk_precheck_aborts(tmp_path, monkeypatch):
    _make_fdb(tmp_path, size=1_000_000)
    (tmp_path / "gbak").write_text("x")
    s = _settings(tmp_path, backup_min_free_disk_multiplier=2.5)
    paths = _paths(tmp_path)

    # disco com pouquíssimo espaço livre
    monkeypatch.setattr(
        gcs_backup.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=1, used=1, free=10),
    )
    ran = {"v": False}
    monkeypatch.setattr(gcs_backup.subprocess, "run",
                        lambda *a, **k: ran.__setitem__("v", True))
    with pytest.raises(BackupError, match="disco insuficiente"):
        gcs_backup.produce_backup(s, paths, log)
    assert ran["v"] is False  # nem chega a rodar o gbak


# --------------------------------------------------------------------------- #
# upload: headers limpos (sem Authorization/X-API-Key), data=file (streaming),
# timeout longo; non-2xx -> BackupError
# --------------------------------------------------------------------------- #
def test_upload_clean_headers_and_streaming(tmp_path, monkeypatch):
    gz = tmp_path / "x.fbk.gz"
    gz.write_bytes(b"gzbytes" * 1000)
    captured = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["data_is_file"] = hasattr(data, "read")  # streaming = file object
        return _Resp({}, status=200)

    monkeypatch.setattr(gcs_backup.requests, "put", fake_put)
    gcs_backup.upload_to_signed_url(
        "https://gcs/signed", gz, "application/gzip", 600, log
    )
    # APENAS Content-Type — sem auth da sessão compartilhada
    assert captured["headers"] == {"Content-Type": "application/gzip"}
    assert "Authorization" not in captured["headers"]
    assert "X-API-Key" not in captured["headers"]
    assert captured["data_is_file"] is True              # streaming
    assert captured["timeout"][1] == 600                  # read timeout longo


def test_upload_non_2xx_raises(tmp_path, monkeypatch):
    gz = tmp_path / "x.fbk.gz"
    gz.write_bytes(b"data")
    monkeypatch.setattr(
        gcs_backup.requests, "put",
        lambda *a, **k: _Resp({}, content=b"denied", status=403),
    )
    with pytest.raises(BackupError, match="403"):
        gcs_backup.upload_to_signed_url("https://gcs", gz, "application/gzip", 600, log)


# --------------------------------------------------------------------------- #
# fluxo feliz: ordem upload-url -> PUT -> confirm, temp limpo, status ok=True
# --------------------------------------------------------------------------- #
def _patch_pipeline_ok(tmp_path, monkeypatch, put_results):
    """Patch gbak + requests.put. put_results: lista de status codes por chamada."""
    _make_fdb(tmp_path)
    (tmp_path / "gbak").write_text("x")

    def fake_run(argv, **kw):
        Path(argv[-1]).write_bytes(b"FBK" * 500)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gcs_backup.subprocess, "run", fake_run)
    monkeypatch.setattr(gcs_backup.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=1, used=0, free=10**12))

    seq = iter(put_results)
    put_calls = {"n": 0}

    def fake_put(url, data=None, headers=None, timeout=None):
        put_calls["n"] += 1
        if data is not None and hasattr(data, "read"):
            data.read()  # consome o stream
        return _Resp({}, content=b"", status=next(seq))

    monkeypatch.setattr(gcs_backup.requests, "put", fake_put)
    return put_calls


def test_run_backup_happy_path(tmp_path, monkeypatch):
    put_calls = _patch_pipeline_ok(tmp_path, monkeypatch, [200])
    s = _settings(tmp_path)
    paths = _paths(tmp_path)
    http = _Http(
        upload_resp={"upload_url": "https://gcs/signed",
                     "object_path": "backups/farm1/2026-06-24.fbk.gz",
                     "content_type": "application/gzip"},
        confirm_resp={"data": {"id": 7, "farm_id": "farm1",
                               "object_path": "backups/farm1/2026-06-24.fbk.gz",
                               "size_bytes_real": 999}},
    )

    status = gcs_backup.run_backup(s, paths, http, log)

    assert status["ok"] is True
    assert status["id"] == 7
    assert status["size_bytes_real"] == 999
    # ordem: upload-url -> PUT -> confirm
    assert [c[1] for c in http.calls] == [
        "/api/integracoes/backups/upload-url",
        "/api/integracoes/backups/confirm",
    ]
    assert put_calls["n"] == 1
    # temp limpo: nenhum .fbk/.gz remanescente
    temp = gcs_backup.resolve_backup_temp(s, paths)
    assert list(temp.glob("*.fbk*")) == []
    # status persistido
    saved = json.loads((paths.backup_dir / "last_backup.json").read_text())
    assert saved["ok"] is True


# --------------------------------------------------------------------------- #
# retry: signed URL nova por tentativa; gbak roda UMA vez
# --------------------------------------------------------------------------- #
def test_run_backup_retry_new_url_per_attempt_gbak_once(tmp_path, monkeypatch):
    # 1ª PUT 503, 2ª PUT 200
    put_calls = _patch_pipeline_ok(tmp_path, monkeypatch, [503, 200])
    monkeypatch.setattr(gcs_backup.time, "sleep", lambda *_: None)

    # conta quantas vezes o gbak roda
    gbak_runs = {"n": 0}
    orig_run = gcs_backup.subprocess.run

    def counting_run(argv, **kw):
        gbak_runs["n"] += 1
        return orig_run(argv, **kw)

    monkeypatch.setattr(gcs_backup.subprocess, "run", counting_run)

    s = _settings(tmp_path, backup_max_retries=3)
    paths = _paths(tmp_path)
    http = _Http(
        upload_resp={"upload_url": "https://gcs/signed",
                     "object_path": "backups/farm1/2026-06-24.fbk.gz"},
        confirm_resp={"data": {"id": 1, "size_bytes_real": 5}},
    )

    status = gcs_backup.run_backup(s, paths, http, log)
    assert status["ok"] is True
    assert gbak_runs["n"] == 1            # gbak UMA vez só
    assert put_calls["n"] == 2            # PUT tentado 2x
    # 2 upload-url (uma por tentativa) + 1 confirm
    paths_called = [c[1] for c in http.calls]
    assert paths_called.count("/api/integracoes/backups/upload-url") == 2
    assert paths_called.count("/api/integracoes/backups/confirm") == 1


# --------------------------------------------------------------------------- #
# retry esgotado -> ok=False, sem levantar, temp limpo
# --------------------------------------------------------------------------- #
def test_run_backup_retry_exhausted_no_raise(tmp_path, monkeypatch):
    _patch_pipeline_ok(tmp_path, monkeypatch, [500, 500, 500])
    monkeypatch.setattr(gcs_backup.time, "sleep", lambda *_: None)
    s = _settings(tmp_path, backup_max_retries=3)
    paths = _paths(tmp_path)
    http = _Http(
        upload_resp={"upload_url": "https://gcs/signed",
                     "object_path": "backups/farm1/2026-06-24.fbk.gz"},
        confirm_resp={"data": {}},
    )

    status = gcs_backup.run_backup(s, paths, http, log)  # não levanta
    assert status["ok"] is False
    assert "error" in status
    # confirm nunca foi chamado (PUT sempre falhou)
    assert all(not c[1].endswith("/confirm") for c in http.calls)
    # temp limpo mesmo no caminho de falha
    temp = gcs_backup.resolve_backup_temp(s, paths)
    assert list(temp.glob("*.fbk*")) == []


# --------------------------------------------------------------------------- #
# data_referencia usa o DIA LOCAL (offset), não UTC
# --------------------------------------------------------------------------- #
def test_run_backup_uses_local_date(tmp_path, monkeypatch):
    _patch_pipeline_ok(tmp_path, monkeypatch, [200])
    # força o "dia local" para um valor fixo conhecido
    monkeypatch.setattr(gcs_backup.timewin, "local_today", lambda s, now_utc=None: "2026-06-25")
    s = _settings(tmp_path)
    paths = _paths(tmp_path)
    http = _Http(
        upload_resp={"upload_url": "https://gcs/signed", "object_path": "b/2026-06-25.fbk.gz"},
        confirm_resp={"data": {"id": 1}},
    )

    status = gcs_backup.run_backup(s, paths, http, log)

    assert status["data_referencia"] == "2026-06-25"
    # a chamada upload-url carrega o data_referencia local
    upload_meta = next(c[2] for c in http.calls if c[1].endswith("/upload-url"))
    assert upload_meta["data_referencia"] == "2026-06-25"


# --------------------------------------------------------------------------- #
# backup_done_today: gating do catch-up
# --------------------------------------------------------------------------- #
def test_backup_done_today(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(gcs_backup.timewin, "local_today", lambda s, now_utc=None: "2026-06-25")
    status_file = paths.backup_dir / "last_backup.json"

    # sem arquivo -> não feito
    assert gcs_backup.backup_done_today(paths, s) is False
    # OK no dia de hoje -> feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-06-25"}))
    assert gcs_backup.backup_done_today(paths, s) is True
    # OK mas de ONTEM -> não feito
    status_file.write_text(json.dumps({"ok": True, "data_referencia": "2026-06-24"}))
    assert gcs_backup.backup_done_today(paths, s) is False
    # de hoje mas FALHOU -> não feito
    status_file.write_text(json.dumps({"ok": False, "data_referencia": "2026-06-25"}))
    assert gcs_backup.backup_done_today(paths, s) is False


# --------------------------------------------------------------------------- #
# parsing de [backup]
# --------------------------------------------------------------------------- #
def test_backup_config_parsing(tmp_path, monkeypatch):
    from syncronizer import configio
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {
        "backup_enabled": True,
        "backup_hour": 5,
        "backup_minute": 30,
        "backup_compression": "xz",
        "backup_max_retries": 5,
        "backup_min_free_disk_multiplier": 3.0,
    })
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.backup_enabled is True
    assert s.backup_hour == 5 and s.backup_minute == 30
    assert s.backup_compression == "xz"
    assert s.backup_max_retries == 5
    assert s.backup_min_free_disk_multiplier == 3.0


def test_backup_section_flattens(tmp_path, monkeypatch):
    """[backup] enabled = true deve resolver backup_enabled."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.toml").write_text(
        "[backup]\nenabled = true\nhour = 7\ncompression = \"gzip\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.backup_enabled is True
    assert s.backup_hour == 7


# --------------------------------------------------------------------------- #
# gating do scheduler
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
    cycles = {"n": 0}
    app = SimpleNamespace(
        settings=s, had_successful_cycle=False, restart_requested=False,
        run_cycle=lambda: cycles.__setitem__("n", cycles["n"] + 1),
        close=lambda: None, paths=SimpleNamespace(), http=None,
    )
    try:
        scheduler.run_service(app, log)
    except SystemExit:
        pass
    return created["sched"], cycles


def test_scheduler_adds_backup_jobs_only_when_enabled(monkeypatch):
    on, _ = _run_service(monkeypatch, backup_enabled=True)
    assert "backup" in on.jobs and "backup_catchup" in on.jobs
    off, _ = _run_service(monkeypatch, backup_enabled=False)
    assert "backup" not in off.jobs and "backup_catchup" not in off.jobs


def test_scheduler_etl_job_respects_window(monkeypatch):
    import syncronizer.scheduler as scheduler

    sched, cycles = _run_service(monkeypatch, run_on_start=False)
    etl = sched.funcs["etl"]

    # fora da janela: não roda o ciclo
    monkeypatch.setattr(scheduler.timewin, "within_window", lambda s, now_utc=None: False)
    etl()
    assert cycles["n"] == 0
    # dentro da janela: roda
    monkeypatch.setattr(scheduler.timewin, "within_window", lambda s, now_utc=None: True)
    etl()
    assert cycles["n"] == 1
