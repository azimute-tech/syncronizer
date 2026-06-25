"""Service loop: a BlockingScheduler with two jobs (ETL + self-update).

A single-worker executor + ``max_instances=1`` serializes everything, so the
SQLite connection is only ever touched by one thread at a time. A self-update
requests a restart, which is honored at the next cycle boundary by exiting 0 so
NSSM relaunches the fresh code.
"""
from __future__ import annotations

import signal
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from . import bootgate, timewin, updater
from .web import server as webserver


def run_service(app, log) -> None:
    s = app.settings

    # Start the localhost admin UI (config/logs/endpoints). Runs even with no config.
    webserver.start(app, log)

    executors = {"default": ThreadPoolExecutor(1)}
    job_defaults = {"coalesce": True, "max_instances": 1,
                    "misfire_grace_time": s.misfire_grace_time}
    sched = BlockingScheduler(timezone="UTC", executors=executors, job_defaults=job_defaults)

    now = datetime.now(timezone.utc)
    started = now
    grace = timedelta(minutes=s.boot_grace_minutes)
    health = {"marked": False}

    def maybe_mark_healthy():
        # Enshrine the running commit as last-known-good only after a real cycle has
        # SUCCEEDED and the process has stayed up for the grace period — never on mere
        # liveness, or a commit that fails every cycle would poison the rollback target.
        if health["marked"] or not app.had_successful_cycle:
            return
        if datetime.now(timezone.utc) - started >= grace:
            bootgate.mark_healthy(app.paths, log)
            health["marked"] = True

    def etl_job():
        # Janela rígida: fora de [start, end) local não há tráfego à API (nem leitura
        # do Firebird). O backlog fica durável no control.db e escoa no próximo ciclo
        # dentro da janela. Vale para o interval E para o startup job (ambos reusam isto).
        if not timewin.within_window(s):
            log.info("fora da janela de execução (%02d–%02d local); pulando ciclo",
                     s.etl_window_start_hour, s.etl_window_end_hour)
            return
        app.run_cycle()
        maybe_mark_healthy()
        if app.restart_requested:
            log.info("restart requested; shutting down scheduler for relaunch")
            sched.shutdown(wait=False)

    def update_job():
        try:
            updater.check_and_update(app, log)
        except Exception as exc:  # noqa: BLE001
            log.exception("update job error: %s", exc)

    def backup_job():
        # run_backup nunca levanta (captura tudo internamente), mas envolvemos em
        # try/except por garantia para nunca derrubar o scheduler.
        try:
            from .backup import run_backup
            run_backup(app.settings, app.paths, app.http, log)
        except Exception as exc:  # noqa: BLE001
            log.exception("backup job error: %s", exc)

    def backup_catchup_job():
        # Recupera o backup do dia se as 20:00 falharam (ex.: internet fora). Só dispara
        # depois do horário agendado e só se o backup de hoje ainda não foi OK; o Lock
        # module-level + o gating por dia evitam duplicar. Após a meia-noite local vira
        # outro dia (hora < backup_hour) e para sozinho até o cron do próximo dia.
        try:
            from .backup import backup_done_today
            ln = timewin.local_now(s)
            after_time = (ln.hour, ln.minute) >= (s.backup_hour, s.backup_minute)
            if after_time and not backup_done_today(app.paths, s):
                log.info("backup: catch-up disparado (backup de hoje ainda não concluído)")
                backup_job()
        except Exception as exc:  # noqa: BLE001
            log.exception("backup catch-up error: %s", exc)

    def restart_watch():
        # honor a restart requested by the admin UI (or self-update) promptly
        if app.restart_requested:
            log.info("restart requested; shutting down scheduler for relaunch")
            sched.shutdown(wait=False)

    sched.add_job(restart_watch, "interval", seconds=15, id="restart_watch")
    sched.add_job(etl_job, "interval", minutes=s.cycle_minutes, id="etl")
    if s.auto_update:
        sched.add_job(update_job, "interval", minutes=s.update_minutes, id="update")
    if s.backup_enabled:
        # backup_hour/minute são horário LOCAL; o cron do scheduler é UTC -> converte.
        bh, bm = timewin.backup_utc_hm(s)
        sched.add_job(backup_job, "cron", hour=bh, minute=bm, id="backup")
        sched.add_job(backup_catchup_job, "interval", minutes=30, id="backup_catchup")
        log.info("backup agendado: %02d:%02d local (=%02d:%02d UTC); catch-up a cada 30min (compression=%s)",
                 s.backup_hour, s.backup_minute, bh, bm, s.backup_compression)
    if s.run_on_start:
        sched.add_job(etl_job, "date", run_date=now + timedelta(seconds=1), id="startup")

    def _graceful(signum, _frame):
        log.info("signal %s received; shutting down gracefully", signum)
        try:
            sched.shutdown(wait=True)
        except Exception:  # noqa: BLE001
            pass

    signal.signal(signal.SIGINT, _graceful)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _graceful)

    janela = (f"{s.etl_window_start_hour:02d}–{s.etl_window_end_hour:02d} local"
              if s.etl_window_enabled else "24h (desabilitada)")
    log.info("scheduler starting: cycle=%dm update=%dm auto_update=%s run_on_start=%s janela=%s",
             s.cycle_minutes, s.update_minutes, s.auto_update, s.run_on_start, janela)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        pass
    finally:
        app.close()
        if app.restart_requested:
            log.info("exiting 0 to apply self-update via service-manager restart")
            sys.exit(0)
