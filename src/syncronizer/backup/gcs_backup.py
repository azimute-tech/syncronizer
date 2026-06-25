"""Lógica do backup do Firebird para o GCS via signed URL.

Ponto crítico (consistência): o backup é feito com ``gbak`` (backup LÓGICO da
engine, transação consistente), nunca cópia crua do ``.fdb`` (que estaria
inconsistente com o servidor rodando). Se o ``gbak`` faltar, ABORTA com log claro.

Segurança: a senha do Firebird NUNCA aparece em log (``_redact_argv``). O PUT pro
GCS usa ``requests.put`` NOVO, sem os headers ``Authorization``/``X-API-Key``/json
da sessão compartilhada — senão a assinatura v4 do GCS falha. As chamadas
``upload-url``/``confirm`` usam o ``app.http`` autenticado (como ``animais.py``).

``run_backup`` NUNCA levanta: captura tudo, loga, grava status em
``state/backup/last_backup.json`` e SEMPRE limpa o ``.fbk``/``.fbk.gz`` no
``finally``. Um :class:`threading.Lock` module-level serializa cron + disparo
manual (UI) para não sobrepor dois backups.
"""
from __future__ import annotations

import hashlib
import gzip
import json
import lzma
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .. import timewin

# Chunk do streaming de compressão: 1 MiB mantém a memória constante mesmo para
# um .fbk de centenas de MB.
_CHUNK = 1024 * 1024

# Conexão (TCP) do PUT; a leitura usa settings.backup_upload_read_timeout.
_PUT_CONNECT_TIMEOUT = 30

# Caminhos onde o gbak do Firebird 2.5 costuma estar quando gbak_path é vazio.
_GBAK_CANDIDATES = (
    r"C:\Program Files\Firebird\Firebird_2_5\bin\gbak.exe",
    r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\gbak.exe",
)

# Serializa cron + "Executar backup agora" da UI: um backup por vez.
_LOCK = threading.Lock()


class BackupError(RuntimeError):
    """Falha esperada do backup (gbak ausente, disco cheio, upload non-2xx, ...)."""


def _redact_argv(argv) -> str:
    """Renderiza o argv do gbak para log SEM expor a senha (-password <segredo>)."""
    out = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        out.append(str(token))
        if str(token).lower() == "-password":
            redact_next = True
    return " ".join(out)


def resolve_gbak_path(settings) -> Path:
    """Resolve o caminho do gbak; levanta :class:`BackupError` se ausente.

    Usa ``settings.backup_gbak_path`` quando informado; senão tenta os caminhos
    padrão do Firebird 2.5 (Program Files e Program Files (x86)).
    """
    configured = getattr(settings, "backup_gbak_path", None)
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
        raise BackupError(
            f"gbak não encontrado no caminho configurado: {p}. "
            "Ajuste [backup].gbak_path no config.toml."
        )
    for candidate in _GBAK_CANDIDATES:
        p = Path(candidate)
        if p.is_file():
            return p
    raise BackupError(
        "gbak não encontrado. Instale o Firebird 2.5 ou defina [backup].gbak_path "
        f"no config.toml. Procurado em: {', '.join(_GBAK_CANDIDATES)}."
    )


def resolve_backup_temp(settings, paths) -> Path:
    """Resolve o diretório de trabalho do .fbk/.gz (default = ``state/backup``)."""
    configured = getattr(settings, "backup_temp_dir", None)
    temp = Path(configured) if configured else paths.backup_dir
    temp.mkdir(parents=True, exist_ok=True)
    return temp


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_status(paths) -> dict | None:
    """Lê state/backup/last_backup.json (ou None se ausente/ilegível)."""
    try:
        with open(paths.backup_dir / "last_backup.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def backup_done_today(paths, settings) -> bool:
    """True se já existe um backup OK para o dia útil LOCAL de hoje.

    Base do catch-up: evita repetir o backup quando as 20:00 já tiveram sucesso (o
    data_referencia é o dia local, igual ao usado por run_backup).
    """
    st = read_status(paths)
    return bool(st and st.get("ok") and st.get("data_referencia") == timewin.local_today(settings))


def _sweep_orphans(temp: Path, log) -> None:
    """Remove .fbk/.fbk.gz/.fbk.xz órfãos (de um restart no meio de um backup)."""
    for pattern in ("*.fbk", "*.fbk.gz", "*.fbk.xz"):
        for f in temp.glob(pattern):
            try:
                f.unlink()
                log.info("backup: removido órfão %s", f.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("backup: não consegui remover órfão %s: %s", f.name, exc)


def produce_backup(settings, paths, log) -> Path:
    """Roda o ``gbak`` e retorna o caminho do ``.fbk`` gerado.

    Pré-check de disco com :func:`shutil.disk_usage` (>= multiplicador * tamanho
    do .fdb). Levanta :class:`BackupError` se o gbak faltar, o disco for
    insuficiente, ou o gbak retornar nonzero/timeout. NUNCA copia o .fdb cru.
    """
    gbak = resolve_gbak_path(settings)
    fdb_path = settings.require_firebird_path()
    temp = resolve_backup_temp(settings, paths)

    # Pré-check de disco: o .fbk pode chegar ao tamanho do .fdb; exigir folga.
    try:
        fdb_size = Path(fdb_path).stat().st_size
    except OSError:
        fdb_size = 0
    if fdb_size:
        needed = int(fdb_size * settings.backup_min_free_disk_multiplier)
        free = shutil.disk_usage(temp).free
        if free < needed:
            raise BackupError(
                f"disco insuficiente em {temp}: livre={free} bytes, "
                f"necessário>={needed} bytes ({settings.backup_min_free_disk_multiplier}x "
                f"o .fdb de {fdb_size} bytes)."
            )

    fbk = temp / f"{settings.backup_db_alias}_{timewin.local_today(settings)}.fbk"
    # gbak conectando via Firebird Service ("localhost/<port>:<fdb>") — backup
    # consistente da engine, não cópia de arquivo. -g inibe GC, -t transação.
    source = f"localhost/{settings.firebird_port}:{fdb_path}"
    argv = [
        str(gbak), "-b", "-g", "-t",
        "-user", settings.firebird_user,
        "-password", settings.firebird_password,
        source, str(fbk),
    ]
    log.info("backup: rodando gbak -> %s", fbk.name)
    log.info("backup: %s", _redact_argv(argv))
    # gbak pode demorar; folga generosa proporcional ao read timeout do upload.
    gbak_timeout = max(1800, settings.backup_upload_read_timeout * 3)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=gbak_timeout
        )
    except FileNotFoundError as exc:  # gbak sumiu entre o resolve e o run
        raise BackupError(f"gbak não pôde ser executado: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"gbak excedeu o timeout de {gbak_timeout}s"
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()[:1000]
        raise BackupError(f"gbak falhou (rc={proc.returncode}): {stderr}")
    if not fbk.is_file() or fbk.stat().st_size == 0:
        raise BackupError("gbak retornou 0 mas o .fbk não foi gerado (ou está vazio).")
    log.info("backup: gbak ok (%d bytes)", fbk.stat().st_size)
    return fbk


def compress(src: Path, dst: Path, settings, log) -> tuple[Path, int, str]:
    """Comprime ``src`` -> ``dst`` em streaming e devolve (dst, tamanho, sha256).

    gzip nível 9 por padrão; xz (lzma) quando ``settings.backup_compression ==
    "xz"``. O sha256 é do arquivo COMPRIMIDO (o que vai no PUT e no confirm).
    Chunks de 1 MiB mantêm a memória constante.
    """
    mode = (settings.backup_compression or "gzip").lower()
    digest = hashlib.sha256()
    size = 0
    if mode == "xz":
        opener = lambda: lzma.open(dst, "wb", preset=9)  # noqa: E731
    else:
        opener = lambda: gzip.open(dst, "wb", compresslevel=9)  # noqa: E731
    log.info("backup: comprimindo (%s) %s -> %s", mode, src.name, dst.name)
    with open(src, "rb") as fin, opener() as fout:
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
    # sha256 + tamanho do arquivo final (comprimido).
    with open(dst, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    sha = digest.hexdigest()
    log.info("backup: comprimido (%d bytes, sha256=%s…)", size, sha[:12])
    return dst, size, sha


def request_upload_url(http, meta: dict) -> dict:
    """POST /api/integracoes/backups/upload-url (farm token via app.http)."""
    resp = http.request(
        "POST", "/api/integracoes/backups/upload-url", json=meta
    )
    return resp.json() if resp.content else {}


def upload_to_signed_url(upload_url: str, file_path: Path, content_type: str,
                         timeout: int, log) -> None:
    """PUT do arquivo comprimido direto pro GCS via signed URL.

    Sessão NOVA (``requests.put``) com APENAS o header ``Content-Type`` — sem
    ``Authorization``/``X-API-Key``/json da sessão compartilhada, senão a
    assinatura v4 do GCS falha. ``data`` é o objeto de arquivo (streaming, memória
    constante). Levanta :class:`BackupError` se o status não for 2xx.
    """
    log.info("backup: PUT %d bytes -> GCS", file_path.stat().st_size)
    with open(file_path, "rb") as fh:
        resp = requests.put(
            upload_url,
            data=fh,
            headers={"Content-Type": content_type},
            timeout=(_PUT_CONNECT_TIMEOUT, timeout),
        )
    if not (200 <= resp.status_code < 300):
        body = ""
        try:
            body = resp.text[:300]
        except Exception:  # noqa: BLE001
            pass
        raise BackupError(f"upload pro GCS falhou (status {resp.status_code}): {body}")
    log.info("backup: upload ok (status %d)", resp.status_code)


def confirm(http, payload: dict) -> dict:
    """POST /api/integracoes/backups/confirm (farm token via app.http)."""
    resp = http.request(
        "POST", "/api/integracoes/backups/confirm", json=payload
    )
    return resp.json() if resp.content else {}


def _write_status(paths, status: dict, log) -> None:
    """Grava o status do último backup em state/backup/last_backup.json."""
    try:
        paths.backup_dir.mkdir(parents=True, exist_ok=True)
        target = paths.backup_dir / "last_backup.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 - status é diagnóstico, nunca quebra o job
        log.warning("backup: não consegui gravar last_backup.json: %s", exc)


def _upload_triplet(http, gz_path: Path, size: int, sha: str, data_ref: str,
                    content_type: str, settings, log) -> dict:
    """upload-url -> PUT -> confirm. Pede signed URL NOVA a cada chamada.

    O caller faz o retry chamando isto de novo (gbak roda UMA vez; cada tentativa
    deve pedir uma signed URL nova porque a anterior pode ter expirado).
    """
    meta = {
        "data_referencia": data_ref,
        "size_bytes": size,
        "sha256": sha,
        "content_type": content_type,
    }
    issued = request_upload_url(http, meta)
    upload_url = issued.get("upload_url")
    object_path = issued.get("object_path")
    if not upload_url or not object_path:
        raise BackupError(f"resposta inválida de upload-url: {issued}")

    upload_to_signed_url(
        upload_url, gz_path, content_type, settings.backup_upload_read_timeout, log
    )

    payload = {
        "object_path": object_path,
        "size_bytes": size,
        "sha256": sha,
        "data_referencia": data_ref,
    }
    return confirm(http, payload)


def run_backup(settings, paths, http, log) -> dict:
    """Orquestra o backup completo. NUNCA levanta — devolve um dict de status.

    Ordem: sweep de órfãos -> gbak (UMA vez) -> compress -> [upload-url -> PUT ->
    confirm] com retry (signed URL nova por tentativa) -> grava status -> limpa
    .fbk/.gz no finally. Serializado por um :class:`threading.Lock` module-level.
    """
    if not _LOCK.acquire(blocking=False):
        log.warning("backup: já há um backup em andamento; ignorando este disparo")
        return {"ok": False, "error": "backup já em andamento", "skipped": True}

    data_ref = timewin.local_today(settings)
    started = _now_iso()
    fbk = None
    gz = None
    status: dict = {"ok": False, "data_referencia": data_ref, "started_at": started}
    ext = "xz" if (settings.backup_compression or "gzip").lower() == "xz" else "gz"
    content_type = "application/gzip"
    try:
        temp = resolve_backup_temp(settings, paths)
        _sweep_orphans(temp, log)

        fbk = produce_backup(settings, paths, log)
        # ".fbk" + ".gz"/".xz" sem with_suffix (que varia entre versões do Python
        # com sufixos multi-ponto). O object_path no servidor é sempre .fbk.gz.
        gz = fbk.parent / f"{fbk.name}.{ext}"
        gz, size, sha = compress(fbk, gz, settings, log)

        last_exc = None
        confirmed = None
        for attempt in range(1, settings.backup_max_retries + 1):
            try:
                confirmed = _upload_triplet(
                    http, gz, size, sha, data_ref, content_type, settings, log
                )
                break
            except Exception as exc:  # noqa: BLE001 - tenta de novo com URL nova
                last_exc = exc
                detail = str(exc)
                resp = getattr(exc, "response", None)
                if resp is not None:
                    try:
                        detail = f"{resp.status_code}: {resp.text[:300]}"
                    except Exception:  # noqa: BLE001
                        pass
                log.warning("backup: tentativa %d/%d falhou: %s",
                            attempt, settings.backup_max_retries, detail)
                if attempt < settings.backup_max_retries:
                    time.sleep(min(30, 2 ** attempt))

        if confirmed is None:
            raise BackupError(f"upload esgotou {settings.backup_max_retries} tentativas: {last_exc}")

        record = (confirmed or {}).get("data", {})
        status.update({
            "ok": True,
            "size_bytes": size,
            "sha256": sha,
            "object_path": record.get("object_path"),
            "size_bytes_real": record.get("size_bytes_real"),
            "id": record.get("id"),
            "finished_at": _now_iso(),
        })
        log.info("backup: concluído (data=%s, %d bytes)", data_ref, size)
    except Exception as exc:  # noqa: BLE001 - nunca levanta; grava status e segue
        status.update({"ok": False, "error": str(exc), "finished_at": _now_iso()})
        log.error("backup: FALHOU (data=%s): %s", data_ref, exc)
    finally:
        for f in (fbk, gz):
            if f is not None:
                try:
                    if Path(f).exists():
                        Path(f).unlink()
                        log.info("backup: limpou %s", Path(f).name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("backup: não consegui limpar %s: %s", f, exc)
        _write_status(paths, status, log)
        _LOCK.release()
    return status
