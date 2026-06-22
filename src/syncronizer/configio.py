"""Read/write the user-managed config.toml safely (for the admin UI).

Writes FLAT TOML (``firebird_path = "..."`` at top level) — the loader's
GroupedTomlSource reads top-level keys as bare field names, so flat == grouped for
our purposes. Path fields are normalized to forward slashes (valid in TOML AND on
Windows, since pathlib normalizes them), and every string is TOML-escaped — so the
backslash/encoding bugs that plagued hand-edited config simply cannot happen.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

# (key, label, type, group, is_path, is_secret) — drives the UI form.
FIELDS = [
    ("firebird_path", "Caminho do banco Firebird (.fdb)", "text", "Firebird", True, False),
    ("firebird_host", "Host", "text", "Firebird", False, False),
    ("firebird_port", "Porta", "int", "Firebird", False, False),
    ("firebird_user", "Usuário", "text", "Firebird", False, False),
    ("firebird_password", "Senha", "password", "Firebird", False, True),
    ("firebird_charset", "Charset (UTF8 / WIN1252 / NONE)", "text", "Firebird", False, False),
    ("api_base_url", "URL base da API", "text", "API", False, False),
    ("api_key", "API Key", "password", "API", False, True),
    ("api_key_header", "Header da API Key (ex.: X-API-Key)", "text", "API", False, False),
    ("api_token", "Bearer token (opcional)", "password", "API", False, True),
    ("cycle_minutes", "Intervalo do ciclo (min)", "int", "Sincronização", False, False),
    ("batch_size", "Tamanho do lote por ciclo", "int", "Sincronização", False, False),
    ("auto_update", "Auto-update via git", "bool", "Atualização", False, False),
    ("update_branch", "Branch do git", "text", "Atualização", False, False),
    ("update_minutes", "Intervalo do auto-update (min)", "int", "Atualização", False, False),
]

_TYPES = {f[0]: f[2] for f in FIELDS}
_PATH_FIELDS = {f[0] for f in FIELDS if f[4]}
_SECRET_FIELDS = {f[0] for f in FIELDS if f[5]}


def read_flat(path: Path) -> dict:
    """Read config.toml into a flat {field: value} dict (tolerates grouped sections)."""
    if not path or not Path(path).is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except Exception:  # noqa: BLE001 - a broken file shouldn't break the UI read
        return {}
    flat: dict = {}
    for key, value in doc.items():
        if isinstance(value, dict):
            for sub_key, sub_val in value.items():
                flat.setdefault(sub_key, sub_val)
                flat[f"{key}_{sub_key}"] = sub_val
        else:
            flat[key] = value
    return flat


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _toml_str("" if value is None else str(value))


def coerce(key: str, value):
    """Coerce a posted form value to the field's type; normalize path separators."""
    kind = _TYPES.get(key, "text")
    if kind == "int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0
    if kind == "bool":
        return value in (True, "true", "True", "on", "1", 1)
    text = "" if value is None else str(value)
    if key in _PATH_FIELDS:
        # forward slashes are valid in TOML and accepted by pathlib on Windows
        text = text.replace("\\", "/").strip()
    return text


def write_flat(path: Path, values: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Gerenciado pela UI do Syncronizer (localhost). Edite pela UI."]
    for key in sorted(values):
        lines.append(f"{key} = {_toml_value(values[key])}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_config(path: Path, posted: dict) -> None:
    """Merge posted fields into the existing config and write it back safely."""
    merged = read_flat(path)
    for key, value in posted.items():
        merged[key] = coerce(key, value)
    write_flat(path, merged)


def form_model(current: dict) -> list:
    """Build the field descriptors + current values for the UI (secrets included —
    this is a localhost-only admin)."""
    out = []
    for key, label, kind, group, is_path, is_secret in FIELDS:
        out.append({
            "key": key, "label": label, "type": kind, "group": group,
            "is_secret": is_secret, "value": current.get(key, ""),
        })
    return out
