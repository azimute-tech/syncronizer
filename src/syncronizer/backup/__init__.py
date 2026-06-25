"""Backup noturno do banco Firebird para o GCS.

Fora de :mod:`syncronizer.endpoints` de propósito — o registry auto-descobre
endpoints ETL ali, e o backup NÃO é um endpoint (não estagia em SQLite, não roda
no ciclo de 10 min). É um job de cron próprio orquestrado por :func:`run_backup`.

Fluxo: ``gbak`` (.fbk consistente) -> ``gzip``/``xz`` em streaming (+ sha256 do
arquivo COMPRIMIDO) -> POST upload-url (farm token) -> PUT direto pro GCS (sem os
headers de auth/json da sessão compartilhada) -> POST confirm.
"""
from .gcs_backup import BackupError, run_backup

__all__ = ["run_backup", "BackupError"]
