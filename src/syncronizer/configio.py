"""Read/write the user-managed config.toml safely (for the admin UI).

Writes FLAT TOML (``firebird_path = "..."`` at top level) — the loader's
GroupedTomlSource reads top-level keys as bare field names, so flat == grouped for
our purposes. Path fields are normalized to forward slashes (valid in TOML AND on
Windows, since pathlib normalizes them), and every string is TOML-escaped — so the
backslash/encoding bugs that plagued hand-edited config simply cannot happen.
"""
from __future__ import annotations

import os
import re
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
    ("etl_window_enabled", "Janela de execução habilitada", "bool", "Sincronização", False, False),
    ("etl_window_start_hour", "Início da janela (hora local)", "int", "Sincronização", False, False),
    ("etl_window_end_hour", "Fim da janela (hora local, exclusivo)", "int", "Sincronização", False, False),
    ("tz_offset_hours", "Offset UTC (-3 = America/Sao_Paulo)", "int", "Sincronização", False, False),
    ("auto_update", "Auto-update via git", "bool", "Atualização", False, False),
    ("update_branch", "Branch do git", "text", "Atualização", False, False),
    ("update_minutes", "Intervalo do auto-update (min)", "int", "Atualização", False, False),
    ("backup_enabled", "Backup noturno habilitado", "bool", "Backup", False, False),
    ("backup_hour", "Hora do backup (horário local; ex.: 20)", "int", "Backup", False, False),
    ("backup_minute", "Minuto do backup (horário local)", "int", "Backup", False, False),
    ("backup_gbak_path", "Caminho do gbak.exe (vazio = auto)", "text", "Backup", True, False),
    ("backup_db_alias", "Prefixo do arquivo (.fbk)", "text", "Backup", False, False),
    ("backup_compression", "Compressão (gzip / xz)", "text", "Backup", False, False),
    ("backup_temp_dir", "Diretório temporário (vazio = padrão)", "text", "Backup", True, False),
    ("backup_max_retries", "Tentativas de upload", "int", "Backup", False, False),
    ("indicadores_enabled", "Envio noturno do CEPEA boi gordo habilitado", "bool", "Indicadores", False, False),
    ("indicadores_hour", "Hora do envio (horário local; ex.: 20)", "int", "Indicadores", False, False),
    ("indicadores_minute", "Minuto do envio (horário local)", "int", "Indicadores", False, False),
]

# Versão do esquema do config.toml. Permite migrar, UMA vez, valores que eram só o
# default antigo gravado no arquivo (o painel grava TODOS os campos do formulário ao
# salvar, então um default vira valor "explícito" no disco sem ninguém ter escolhido).
#   1 (ou ausente) -> 2: CEPEA (`indicadores_enabled`) passa a nascer ligado. Antes da
#   v2 o campo não existia no painel; um `false` no arquivo só podia vir do
#   config.toml.example antigo (default desligado), nunca de uma escolha pela UI.
CONFIG_VERSION = 2
_VERSION_KEY = "config_version"

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
    """Merge posted fields into the existing config and write it back safely.

    Migra antes (no-op se já está na versão atual) e carimba ``config_version`` —
    assim o arquivo criado pelo painel numa instalação nova já nasce na versão atual
    e uma migração futura não confunde a escolha do operador com default antigo.
    """
    migrate_config(path)
    merged = read_flat(path)
    for key, value in posted.items():
        if key == _VERSION_KEY:
            continue  # versão é do esquema, não do formulário
        merged[key] = coerce(key, value)
    merged[_VERSION_KEY] = CONFIG_VERSION
    write_flat(path, merged)


_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
_FLAT_INDICADORES_OFF = re.compile(r"^(\s*indicadores_enabled\s*=\s*)false\b(.*)$")
_GROUPED_ENABLED_OFF = re.compile(r"^(\s*enabled\s*=\s*)false\b(.*)$")


def migrate_config(path: Path) -> list:
    """Migra o config.toml para :data:`CONFIG_VERSION` (idempotente).

    Edita o texto linha a linha (preserva comentários e o formato agrupado ou
    plano), valida o resultado com ``tomllib`` e grava de forma atômica. Arquivo
    ausente é no-op: os defaults do código já valem e o painel carimba a versão no
    primeiro "Salvar". Retorna a lista de mudanças aplicadas (vazia = nada a fazer).
    """
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    doc = tomllib.loads(text)  # TOML quebrado: deixa o loader reportar o erro
    version = doc.get(_VERSION_KEY, 1)
    if not isinstance(version, int) or version >= CONFIG_VERSION:
        return []

    changes: list = []
    lines = text.splitlines(keepends=True)
    out: list = []
    section = None
    for line in lines:
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _SECTION_RE.match(body)
        if m:
            section = m.group(1)
            out.append(line)
            continue
        if version < 2:
            hit = None
            if section is None:
                hit = _FLAT_INDICADORES_OFF.match(body)
            elif section == "indicadores":
                hit = _GROUPED_ENABLED_OFF.match(body)
            if hit:
                body = f"{hit.group(1)}true{hit.group(2)}"
                changes.append("indicadores_enabled: false -> true (default antigo; CEPEA agora ligado)")
        out.append(body + eol)

    # chave de topo precisa vir antes da primeira tabela: entra logo após o
    # cabeçalho de comentários (ou no início do arquivo).
    insert_at = 0
    while insert_at < len(out) and out[insert_at].lstrip().startswith("#"):
        insert_at += 1
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += "\n"
    out.insert(insert_at, f"{_VERSION_KEY} = {CONFIG_VERSION}\n")
    changes.append(f"{_VERSION_KEY}: {version} -> {CONFIG_VERSION}")

    new_text = "".join(out)
    tomllib.loads(new_text)  # nunca grava um arquivo que o loader não leria
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return changes


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
