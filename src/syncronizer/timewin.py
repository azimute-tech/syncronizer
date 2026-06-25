"""Aritmética de fuso/janela por offset fixo (sem dependência de tzdata).

O scheduler roda em UTC; o cliente raciocina em horário local (America/Sao_Paulo).
O Brasil não tem horário de verão desde 2019, então um offset fixo (``tz_offset_hours``,
default -3) é correto e dispensa o pacote ``tzdata`` no Windows + Python 3.9.

Convenção: ``local = utc + offset`` (logo ``utc = local - offset``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def local_now(settings, now_utc: datetime | None = None) -> datetime:
    """Hora local (naive, já deslocada pelo offset) a partir do ``now_utc`` (ou agora)."""
    base = now_utc or datetime.now(timezone.utc)
    return base + timedelta(hours=settings.tz_offset_hours)


def within_window(settings, now_utc: datetime | None = None) -> bool:
    """True se estamos dentro da janela de execução (horário local).

    Janela ``[start, end)`` por hora cheia: ``start`` inclusivo, ``end`` exclusivo
    (07–19 roda 07:00–18:59 e para às 19:00). Suporta janela que cruza a meia-noite
    (ex.: 22–6) por robustez. Desabilitada -> sempre True.
    """
    if not settings.etl_window_enabled:
        return True
    h = local_now(settings, now_utc).hour
    start, end = settings.etl_window_start_hour, settings.etl_window_end_hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def backup_utc_hm(settings) -> tuple[int, int]:
    """Converte o horário LOCAL do backup para (hora, minuto) UTC do cron.

    20:00 local com offset -3 -> 23:00 UTC. O ``% 24`` cobre o wrap (ex.: offset -4).
    """
    utc_hour = (settings.backup_hour - settings.tz_offset_hours) % 24
    return utc_hour, settings.backup_minute


def local_today(settings, now_utc: datetime | None = None) -> str:
    """Data do dia útil local (YYYY-MM-DD) — base do nome do .fbk e do data_referencia."""
    return local_now(settings, now_utc).strftime("%Y-%m-%d")
