"""Boot gate: crash-loop breaker + auto-rollback.

Runs as early as possible at startup — BEFORE the app imports settings/endpoint code
— so a commit that crashes on import or in config is still caught. Each boot
increments a per-ref counter; after ``max_boot_attempts`` consecutive crashed boots
it reverts the working tree to ``last_good_commit``, quarantines the bad ref, and asks
the caller to restart onto the good code. After a *successful* cycle has run for a
grace period, :func:`mark_healthy` records the running commit as good and resets the
counter.

Depends only on stdlib + :mod:`syncronizer.updater` (also stdlib-only), so importing
it cannot fail because of a regression in config/app code.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import updater

_DEFAULT_MAX_ATTEMPTS = 3


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:  # noqa: BLE001
        return default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


def _max_attempts(explicit) -> int:
    if explicit is not None:
        return int(explicit)
    env = os.environ.get("SYNCRONIZER_MAX_BOOT_ATTEMPTS")
    try:
        return int(env) if env else _DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS


def pre_boot(paths, log, max_attempts=None) -> bool:
    """Increment the boot counter; roll back if we're crash-looping.

    Returns True if a rollback checkout was performed and the caller should exit so the
    service manager relaunches onto the good commit.
    """
    sha = updater.current_sha(paths, log=log)
    if sha is None:
        return False  # no git working tree (dev) -> nothing to guard

    attempts = _read_int(paths.boot_attempts, 0) + 1
    _write(paths.boot_attempts, attempts)

    if attempts <= _max_attempts(max_attempts):
        return False

    good = paths.last_good_commit.read_text().strip() if paths.last_good_commit.exists() else ""
    if not good or good == sha:
        log.error("crash-loop on %s (attempt %d) but no distinct good commit to roll back to",
                  sha[:8], attempts)
        return False

    log.error("crash-loop detected on %s (attempt %d); rolling back to %s",
              sha[:8], attempts, good[:8])
    updater.add_quarantine(paths, sha)
    try:
        git_exe = updater.resolve_git(paths)
        updater.git(git_exe, paths.repo_dir, "checkout", "--force", good)
        # Best-effort: realign deps with the good commit's manifest. A forward dep bump
        # from the bad commit is not auto-downgraded, but reinstalling the good manifest
        # restores its pins where possible.
        try:
            updater.pip_install(paths, log)
        except Exception as exc:  # noqa: BLE001
            log.warning("re-pip after rollback failed (continuing): %s", exc)
        _write(paths.boot_attempts, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("rollback checkout failed: %s", exc)
        return False


def mark_healthy(paths, log) -> None:
    """Record the running commit as last-known-good and reset the boot counter.

    Call ONLY after a real cycle has succeeded — never merely because the process is
    still alive — or a commit that fails every cycle would be enshrined as good.
    """
    _write(paths.boot_attempts, 0)
    sha = updater.current_sha(paths, log=log)
    if sha:
        _write(paths.last_good_commit, sha)
        log.info("marked %s as last_good_commit", sha[:8])
