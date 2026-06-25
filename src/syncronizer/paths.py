"""Absolute filesystem layout for the service.

The writable data dir is resolved from the ``SYNCRONIZER_DATA_DIR`` env var (set by
the installer/NSSM on Windows), so the service's working directory (``system32``)
never matters. Every path here is absolute.

This module must NOT import :mod:`syncronizer.config` (it is imported *by* config),
so :func:`build_paths` takes a duck-typed settings object instead of the type.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_DATA_DIR = "SYNCRONIZER_DATA_DIR"


def resolve_data_dir() -> Path:
    """Resolve the writable data root.

    On Windows the data dir is FIXED at ``%PROGRAMDATA%\\Syncronizer`` and the
    ``SYNCRONIZER_DATA_DIR`` env var is deliberately IGNORED: a service environment
    block can get mangled (e.g. NSSM AppEnvironmentExtra with space-containing values),
    and a wrong data dir means the service can't find its config. Hard-coding the
    Windows location removes that whole class of failure.

    The env override remains available for non-Windows development.
    """
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return (Path(base) / "Syncronizer").resolve()
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "data").resolve()


@dataclass(frozen=True)
class Paths:
    """Resolved absolute paths used across the app."""

    data_dir: Path
    config_dir: Path
    config_file: Path
    state_dir: Path
    control_db: Path
    backup_dir: Path
    logs_dir: Path
    log_file: Path
    # self-update state markers
    last_applied_commit: Path
    last_good_commit: Path
    boot_attempts: Path
    quarantine: Path
    # code/runtime locations (installer-written; derived defaults otherwise)
    repo_dir: Path
    venv_dir: Path
    git_exe: Path | None
    nssm_exe: Path | None

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.state_dir, self.backup_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def build_paths(settings, data_dir: Path | None = None) -> Paths:
    """Compute the absolute :class:`Paths` from settings + the resolved data dir.

    ``settings`` is duck-typed (anything exposing ``repo_dir``/``venv_dir``/
    ``git_exe``/``nssm_exe`` optional overrides) and may be ``None`` — in that case
    the code/runtime locations fall back to env vars (set by the installer/NSSM) so
    the boot gate can run before settings are even loaded.
    """
    data_dir = (data_dir or resolve_data_dir())
    config_dir = data_dir / "config"
    state_dir = data_dir / "state"
    logs_dir = data_dir / "logs"

    def _override(attr: str, env_key: str, default: Path | None) -> Path | None:
        val = getattr(settings, attr, None) if settings is not None else None
        if not val:
            val = os.environ.get(env_key)
        return Path(val).resolve() if val else default

    # On Windows, derive the bundled runtime by install convention so config.toml
    # never needs a [paths] section (which kept getting mangled).
    git_default = nssm_default = None
    if os.name == "nt":
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Syncronizer" / "runtime"
        g, n = pf / "git" / "cmd" / "git.exe", pf / "nssm" / "nssm.exe"
        git_default = g if g.exists() else None
        nssm_default = n if n.exists() else None

    return Paths(
        data_dir=data_dir,
        config_dir=config_dir,
        config_file=config_dir / "config.toml",
        state_dir=state_dir,
        control_db=state_dir / "control.db",
        backup_dir=state_dir / "backup",
        logs_dir=logs_dir,
        log_file=logs_dir / "syncronizer.log",
        last_applied_commit=state_dir / "last_applied_commit",
        last_good_commit=state_dir / "last_good_commit",
        boot_attempts=state_dir / "boot_attempts",
        quarantine=state_dir / "quarantine.json",
        repo_dir=_override("repo_dir", "SYNCRONIZER_REPO_DIR", data_dir / "repo"),
        venv_dir=_override("venv_dir", "SYNCRONIZER_VENV_DIR", data_dir / "venv"),
        git_exe=_override("git_exe", "SYNCRONIZER_GIT_EXE", git_default),
        nssm_exe=_override("nssm_exe", "SYNCRONIZER_NSSM_EXE", nssm_default),
    )
