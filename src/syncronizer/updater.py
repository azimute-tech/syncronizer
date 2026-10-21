"""Self-update via git sync.

Runs as a separate scheduler job (``update_minutes``). Pulls the tracked branch of
the public repo with the bundled git, re-installs deps if the manifest changed, and
requests a graceful restart so the fresh interpreter loads the new code. Designed to
be a strict no-op when ``HEAD == origin/<branch>`` (anti-restart-loop guard), and to
quarantine a commit whose dependencies fail to install so it is never re-attempted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEP_FILES = {"requirements.txt", "pyproject.toml"}


class GitError(RuntimeError):
    pass


def resolve_git(paths) -> str:
    """Absolute bundled git on a target; fall back to system ``git`` in dev."""
    git_exe = paths.git_exe
    if git_exe and Path(git_exe).exists():
        return str(git_exe)
    return "git"


def venv_python(paths) -> str:
    venv = paths.venv_dir
    if venv:
        for candidate in (Path(venv) / "Scripts" / "python.exe", Path(venv) / "bin" / "python"):
            if candidate.exists():
                return str(candidate)
    return sys.executable


def _run(git_exe, repo, *args, timeout=120):
    return subprocess.run(
        [str(git_exe), "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def git(git_exe, repo, *args, timeout=120) -> str:
    result = _run(git_exe, repo, *args, timeout=timeout)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def write_marker(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value)
    os.replace(tmp, path)


def read_quarantine(paths) -> set:
    try:
        if paths.quarantine.exists():
            return set(json.loads(paths.quarantine.read_text()))
    except Exception:  # noqa: BLE001
        pass
    return set()


def add_quarantine(paths, sha: str) -> None:
    q = read_quarantine(paths)
    if sha not in q:
        q.add(sha)
        paths.quarantine.parent.mkdir(parents=True, exist_ok=True)
        paths.quarantine.write_text(json.dumps(sorted(q)))


def current_sha(paths, log=None):
    repo = paths.repo_dir
    if not (Path(repo) / ".git").exists():
        return None
    try:
        return git(resolve_git(paths), repo, "rev-parse", "HEAD")
    except Exception as exc:  # noqa: BLE001
        if log:
            log.warning("rev-parse HEAD failed: %s", exc)
        return None


def pip_install(paths, log) -> bool:
    req = Path(paths.repo_dir) / "requirements.txt"
    try:
        result = subprocess.run(
            [venv_python(paths), "-m", "pip", "install", "--no-input",
             "--no-warn-script-location", "-r", str(req)],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode != 0:
            log.error("pip install rc=%s: %s", result.returncode, result.stderr[-2000:])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("pip install error: %s", exc)
        return False


def check_and_update(app, log) -> bool:
    """Returns True if an update was applied and a restart was requested."""
    s = app.settings
    paths = app.paths
    if not s.auto_update:
        return False
    repo = paths.repo_dir
    if not (Path(repo) / ".git").exists():
        log.debug("no git working tree at %s; skipping self-update", repo)
        return False

    git_exe = resolve_git(paths)
    branch = s.update_branch

    try:
        git(git_exe, repo, "fetch", "--prune", "origin", branch, timeout=120)
    except Exception as exc:  # noqa: BLE001 - network/offline tolerance
        log.warning("git fetch failed (offline?); staying on current code: %s", exc)
        return False

    try:
        local = git(git_exe, repo, "rev-parse", "HEAD")
        remote = git(git_exe, repo, "rev-parse", f"origin/{branch}")
    except Exception as exc:  # noqa: BLE001
        log.warning("git rev-parse failed: %s", exc)
        return False

    if local == remote:
        return False  # up-to-date -> NO restart (primary anti-loop guard)

    if remote in read_quarantine(paths):
        log.warning("remote %s is quarantined (prior failed update); not pulling", remote[:8])
        return False

    old = local
    try:
        git(git_exe, repo, "pull", "--ff-only", "origin", branch, timeout=180)
    except GitError as exc:
        log.error("git pull --ff-only failed (rebased/force-pushed upstream?): %s", exc)
        if not s.allow_hard_reset:
            return False
        try:
            git(git_exe, repo, "reset", "--hard", f"origin/{branch}")
            git(git_exe, repo, "clean", "-fdx")
        except Exception as exc2:  # noqa: BLE001
            log.error("hard-reset recovery failed: %s", exc2)
            return False

    new = git(git_exe, repo, "rev-parse", "HEAD")
    if new == old:
        return False

    changed = git(git_exe, repo, "diff", "--name-only", old, new).split()
    if DEP_FILES.intersection(Path(c).name for c in changed):
        if not pip_install(paths, log):
            log.error("dependency install failed for %s; rolling back to %s and quarantining it",
                      new[:8], old[:8])
            add_quarantine(paths, new)  # never re-pull this commit
            try:
                git(git_exe, repo, "reset", "--hard", old)
            except Exception as exc:  # noqa: BLE001
                log.error("rollback reset failed: %s", exc)
            return False

    write_marker(paths.last_applied_commit, new)
    log.info("self-update applied %s -> %s; restart pending", old[:8], new[:8])
    app.request_restart()
    return True
