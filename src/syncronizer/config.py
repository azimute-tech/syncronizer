"""Typed configuration via pydantic-settings.

Precedence (highest first): constructor args > ``SYNC_*`` environment vars >
``.env`` file > ``config/config.toml`` (grouped sections) > defaults.

The grouped TOML (``[firebird] path = ...``) is flattened onto the flat field
names (``firebird_path``) by :class:`GroupedTomlSource` so the on-disk file stays
readable while env vars remain simple (``SYNC_FIREBIRD_PASSWORD``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .paths import resolve_data_dir

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10 dev box
    import tomli as tomllib  # type: ignore[no-redef]


# Sections we flatten as "<section>_<key>" in addition to the bare key.
_KNOWN_SECTIONS = ("firebird", "api", "runtime", "update", "paths", "logging")


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        # Surface a clear, catchable error instead of a raw traceback. A corrupt
        # config.toml lives outside the git tree, so a code rollback cannot fix it —
        # the operator must.
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _flatten_toml(doc: dict) -> dict:
    """Flatten grouped TOML to flat field names.

    For a section ``s`` with key ``k`` we register both ``k`` and ``s_k`` so that
    ``[firebird] path`` resolves ``firebird_path`` while ``[runtime] cycle_minutes``
    resolves ``cycle_minutes``. Section-qualified keys win over bare ones.
    """
    flat: dict[str, Any] = {}
    bare: dict[str, Any] = {}
    for key, value in doc.items():
        if isinstance(value, dict) and key in _KNOWN_SECTIONS:
            for sub_key, sub_val in value.items():
                bare.setdefault(sub_key, sub_val)
                flat[f"{key}_{sub_key}"] = sub_val
        else:
            bare[key] = value
    # bare keys first, section-qualified override
    merged = dict(bare)
    merged.update(flat)
    return merged


class GroupedTomlSource(PydanticBaseSettingsSource):
    """A pydantic-settings source that reads + flattens ``config.toml``."""

    def __init__(self, settings_cls, toml_path: Path):
        super().__init__(settings_cls)
        self._values = _flatten_toml(_read_toml(toml_path))

    def get_field_value(self, field: FieldInfo, field_name: str) -> Tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def prepare_field_value(self, field_name, field, value, value_is_complex):  # noqa: D401
        return value

    def __call__(self) -> dict:
        return {k: v for k, v in self._values.items() if v is not None}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- firebird (secrets; config/env only, never committed) ---
    firebird_path: Optional[Path] = None  # required for run/run-once/self-check
    firebird_host: str = "localhost"
    firebird_port: int = 3050
    firebird_user: str = "SYSDBA"
    firebird_password: str = "masterkey"
    firebird_charset: str = "UTF8"
    # firebirdsql reuses this as the per-packet socket read timeout too, so it must be
    # large enough to cover the slowest inter-packet gap of a busy/locked server, not
    # just the TCP connect. Too low silently aborts slow fetches mid-extract.
    firebird_connect_timeout: float = 60.0

    # --- api ---
    api_base_url: str = ""
    api_key: str = ""                 # sent on every request as api_key_header
    api_key_header: str = "X-API-Key"  # change to whatever your API expects (e.g. "apikey")
    api_token: str = ""               # optional Bearer token (Authorization: Bearer <token>)
    api_timeout: int = 30
    api_max_retries: int = 3

    # --- runtime / scheduling ---
    cycle_minutes: int = 10
    batch_size: int = 500
    misfire_grace_time: int = 300
    run_on_start: bool = True

    # --- self-update (git sync) ---
    auto_update: bool = True
    update_branch: str = "main"
    update_minutes: int = 30
    repo_url: str = ""
    allow_hard_reset: bool = False
    max_boot_attempts: int = 3
    boot_grace_minutes: int = 5

    # --- paths (installer-written overrides; absolute) ---
    git_exe: Optional[Path] = None
    nssm_exe: Optional[Path] = None
    repo_dir: Optional[Path] = None
    venv_dir: Optional[Path] = None

    # --- logging ---
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 7

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        toml_path = resolve_data_dir() / "config" / "config.toml"
        toml_source = GroupedTomlSource(settings_cls, toml_path)
        # init > env > dotenv > toml > secrets
        return (init_settings, env_settings, dotenv_settings, toml_source, file_secret_settings)

    def require_firebird_path(self) -> Path:
        """Return the Firebird path or raise a clear, actionable error."""
        if not self.firebird_path:
            raise ConfigError(
                "Firebird database path is not configured. Set [firebird].path in "
                "config.toml or the SYNC_FIREBIRD_PATH environment variable."
            )
        return Path(self.firebird_path)


class ConfigError(RuntimeError):
    """Raised for missing/invalid configuration with an actionable message."""


def load_settings() -> Settings:
    """Build :class:`Settings` from all configured sources."""
    return Settings()
