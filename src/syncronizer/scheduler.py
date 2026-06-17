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

from . import bootgate, updater


def run_service(app, log) -> None:
    s = app.settings

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

    sched.add_job(etl_job, "interval", minutes=s.cycle_minutes, id="etl")
    if s.auto_update:
        sched.add_job(update_job, "interval", minutes=s.update_minutes, id="update")
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

    log.info("scheduler starting: cycle=%dm update=%dm auto_update=%s run_on_start=%s",
             s.cycle_minutes, s.update_minutes, s.auto_update, s.run_on_start)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        pass
    finally:
        app.close()
        if app.restart_requested:
            log.info("exiting 0 to apply self-update via service-manager restart")
            sys.exit(0)
