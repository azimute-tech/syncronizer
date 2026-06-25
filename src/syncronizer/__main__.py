"""Command-line entry point — shared by dev (`python -m syncronizer`) and the
service (`<venv>\\python.exe -m syncronizer run`).

Subcommands:
  run           start the always-on scheduler (ETL + self-update jobs)
  run-once      execute a single ETL+SEND cycle and exit (first-install validation)
  self-check    import endpoints, open the control DB (+ optionally Firebird)
  print-config  print resolved settings + paths as JSON (secrets redacted)
  install-help  print the NSSM service registration commands

Repo modules are imported lazily inside the subcommands (NOT at module top) so the
crash-loop breaker in ``run`` can execute before importing config/app code that a bad
self-update might have broken.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_SECRET_FIELDS = ("firebird_password", "api_token", "api_key")


def _setup(settings, paths):
    from .logging_setup import setup_logging
    paths.ensure_dirs()
    return setup_logging(paths.log_file, settings.log_level,
                         settings.log_max_bytes, settings.log_backup_count)


def _early_logger(paths) -> logging.Logger:
    """A minimal stdlib-only file logger for the boot gate (no repo deps)."""
    log = logging.getLogger("syncronizer.boot")
    log.propagate = False
    if not log.handlers:
        try:
            paths.logs_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(paths.log_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
        except Exception:  # noqa: BLE001
            log.addHandler(logging.NullHandler())
    return log


def cmd_print_config(_args) -> int:
    from .config import load_settings
    from .paths import build_paths
    settings = load_settings()
    paths = build_paths(settings)
    data = json.loads(settings.model_dump_json())
    for field in _SECRET_FIELDS:
        if data.get(field):
            data[field] = "***redacted***"
    data["_resolved_paths"] = {k: (str(v) if v is not None else None)
                               for k, v in paths.__dict__.items()}
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_self_check(args) -> int:
    from .config import ConfigError, load_settings
    from .paths import build_paths
    settings = load_settings()
    paths = build_paths(settings)
    log = _setup(settings, paths)
    ok = True

    from .core import registry
    endpoints = registry.discover(log=log)
    print(f"endpoints: {len(endpoints)} -> {[e.name for e in endpoints]}")

    from .core.migrations import run_migrations
    from .db.store import ControlStore
    store = ControlStore(paths.control_db, log=log)
    try:
        run_migrations(store, log=log)
        print(f"control.db: OK ({paths.control_db}, schema_version={store.schema_version})")
    finally:
        store.close()

    try:
        path = settings.require_firebird_path()
        from .db.firebird import FirebirdClient, FirebirdUnavailable
        fb = FirebirdClient(settings, log=log)
        try:
            fb.connect()
            print(f"firebird: OK ({settings.firebird_host}:{settings.firebird_port} {path})")
        except FirebirdUnavailable as exc:
            print(f"firebird: UNAVAILABLE ({exc})")
            if args.require_firebird:
                ok = False
        finally:
            fb.close()
    except ConfigError as exc:
        print(f"firebird: NOT CONFIGURED ({exc})")
        if args.require_firebird:
            ok = False

    if settings.backup_enabled:
        from .backup.gcs_backup import (
            BackupError,
            resolve_backup_temp,
            resolve_gbak_path,
        )
        # gbak presente (sem ele o backup é impossível — nunca cai pra cópia crua).
        try:
            gbak = resolve_gbak_path(settings)
            print(f"backup gbak: OK ({gbak})")
        except BackupError as exc:
            print(f"backup gbak: FALTANDO ({exc})")
            ok = False
        # temp dir gravável (probe write).
        try:
            temp = resolve_backup_temp(settings, paths)
            probe = temp / ".selfcheck-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"backup temp: OK ({temp})")
        except Exception as exc:  # noqa: BLE001
            print(f"backup temp: NÃO GRAVÁVEL ({exc})")
            ok = False
        # API configurada (base_url + key/token) — necessária para upload-url/confirm.
        if settings.api_base_url and (settings.api_key or settings.api_token):
            print(f"backup api: OK ({settings.api_base_url})")
        else:
            print("backup api: NÃO CONFIGURADA (defina [api].base_url + key/token)")
            ok = False

    print("SELF-CHECK:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def cmd_run_once(_args) -> int:
    from .app import Application
    from .config import load_settings
    from .paths import build_paths
    settings = load_settings()
    paths = build_paths(settings)
    _setup(settings, paths)
    app = Application(settings=settings)
    try:
        app.run_cycle()
    finally:
        app.close()
    return 0


def cmd_run(_args) -> int:
    # Crash-loop breaker runs FIRST, with only stdlib + paths/bootgate/updater deps, so
    # a regression in config/app code (or logging) from a bad self-update still counts
    # toward the boot-attempt threshold and can be rolled back.
    from . import bootgate
    from .paths import build_paths, resolve_data_dir
    data_dir = resolve_data_dir()
    early_paths = build_paths(None, data_dir=data_dir)
    early_paths.ensure_dirs()
    boot_log = _early_logger(early_paths)
    if bootgate.pre_boot(early_paths, boot_log):
        boot_log.info("rolled back to last-good commit; exiting 0 to relaunch on it")
        return 0

    # Full bootstrap. Any crash from here on was already counted above, so repeated
    # NSSM restarts will eventually trip the rollback.
    from .app import Application
    from .config import load_settings
    from .scheduler import run_service
    settings = load_settings()
    paths = build_paths(settings, data_dir=data_dir)
    log = _setup(settings, paths)
    app = Application(settings=settings)
    run_service(app, log)
    return 0


INSTALL_HELP = r"""
Register the Windows service with NSSM (run from an elevated PowerShell). Paths
assume the installer layout; adjust as needed.

  $NSSM = "C:\Program Files\Syncronizer\runtime\nssm\nssm.exe"
  $PY   = "C:\ProgramData\Syncronizer\venv\Scripts\python.exe"
  $REPO = "C:\ProgramData\Syncronizer\repo"
  $DATA = "C:\ProgramData\Syncronizer"

  & $NSSM install Syncronizer "$PY" -m syncronizer run
  & $NSSM set Syncronizer AppDirectory "$REPO"
  & $NSSM set Syncronizer AppEnvironmentExtra SYNCRONIZER_DATA_DIR=$DATA `
        SYNCRONIZER_REPO_DIR=$REPO `
        SYNCRONIZER_GIT_EXE="C:\Program Files\Syncronizer\runtime\git\cmd\git.exe" `
        SYNCRONIZER_NSSM_EXE=$NSSM `
        SYNCRONIZER_VENV_DIR="$DATA\venv"
  & $NSSM set Syncronizer AppExit Default Restart
  & $NSSM set Syncronizer AppThrottle 60000
  & $NSSM set Syncronizer AppRestartDelay 30000
  & $NSSM set Syncronizer AppStdout "$DATA\logs\service-stdout.log"
  & $NSSM set Syncronizer AppStderr "$DATA\logs\service-stderr.log"
  & $NSSM set Syncronizer AppRotateFiles 1
  & $NSSM set Syncronizer AppRotateBytes 10485760
  & $NSSM set Syncronizer AppStopMethodConsole 15000
  & $NSSM set Syncronizer Start SERVICE_AUTO_START
  & $NSSM start Syncronizer
""".strip()


def cmd_install_help(_args) -> int:
    print(INSTALL_HELP)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="syncronizer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the always-on scheduler").set_defaults(func=cmd_run)
    sub.add_parser("run-once", help="run a single cycle and exit").set_defaults(func=cmd_run_once)

    sc = sub.add_parser("self-check", help="validate endpoints + DBs")
    sc.add_argument("--require-firebird", action="store_true",
                    help="fail if Firebird is not reachable (used by the installer)")
    sc.set_defaults(func=cmd_self_check)

    sub.add_parser("print-config", help="print resolved settings").set_defaults(func=cmd_print_config)
    sub.add_parser("install-help", help="print NSSM service commands").set_defaults(func=cmd_install_help)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001
        # ConfigError gets a clean message + exit 2. Detect it by name to avoid importing
        # config here (which itself might be the thing that failed). Everything else
        # re-raises so the service manager sees a nonzero exit (counted by the boot gate).
        if type(exc).__name__ == "ConfigError":
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
