"""Testes do helper de fuso/janela (timewin) — offset fixo, sem tzdata."""
from datetime import datetime, timezone
from types import SimpleNamespace

from syncronizer import timewin


def _s(**over):
    base = dict(
        tz_offset_hours=-3,
        etl_window_enabled=True,
        etl_window_start_hour=7,
        etl_window_end_hour=19,
        backup_hour=20,
        backup_minute=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _utc(h, m=0):
    return datetime(2026, 6, 25, h, m, tzinfo=timezone.utc)


# --- local_now ------------------------------------------------------------- #
def test_local_now_applies_offset():
    # offset -3: 15:00 UTC -> 12:00 local
    assert timewin.local_now(_s(), _utc(15)).hour == 12
    # wrap pra trás: 01:00 UTC, offset -3 -> 22:00 do dia anterior
    assert timewin.local_now(_s(), _utc(1)).hour == 22


# --- within_window --------------------------------------------------------- #
def test_within_window_inside_and_boundaries():
    s = _s()  # janela 07–19 local, offset -3
    assert timewin.within_window(s, _utc(15)) is True    # 12:00 local
    assert timewin.within_window(s, _utc(9)) is False     # 06:00 local (antes)
    assert timewin.within_window(s, _utc(10)) is True     # 07:00 local (início inclusivo)
    assert timewin.within_window(s, _utc(21)) is True     # 18:00 local (último dentro)
    assert timewin.within_window(s, _utc(22)) is False    # 19:00 local (fim exclusivo)


def test_within_window_disabled_is_always_true():
    s = _s(etl_window_enabled=False)
    assert timewin.within_window(s, _utc(3)) is True       # 00:00 local


def test_within_window_wraps_midnight():
    s = _s(etl_window_start_hour=22, etl_window_end_hour=6)
    assert timewin.within_window(s, _utc(2)) is True        # 23:00 local
    assert timewin.within_window(s, _utc(6)) is True        # 03:00 local
    assert timewin.within_window(s, _utc(15)) is False      # 12:00 local


# --- backup_utc_hm --------------------------------------------------------- #
def test_backup_utc_hm_converts_local_to_utc():
    assert timewin.backup_utc_hm(_s()) == (23, 0)                      # 20:00 BRT -> 23:00 UTC
    assert timewin.backup_utc_hm(_s(tz_offset_hours=-4)) == (0, 0)     # wrap pra meia-noite
    assert timewin.backup_utc_hm(_s(backup_minute=30)) == (23, 30)     # minuto preservado


# --- local_today ----------------------------------------------------------- #
def test_local_today_uses_local_date():
    # 01:00 UTC com offset -3 -> 22:00 do dia anterior -> data local = 2026-06-24
    assert timewin.local_today(_s(), _utc(1)) == "2026-06-24"
    assert timewin.local_today(_s(), _utc(15)) == "2026-06-25"
