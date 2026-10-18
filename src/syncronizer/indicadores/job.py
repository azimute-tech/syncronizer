"""Orquestrador do job noturno de indicadores: CEPEA + DATAGRO, um status só.

Cada fonte roda no seu próprio try/except (:func:`cepea.run_cepea`,
:func:`datagro.run_datagro`) e devolve um bloco; este módulo junta os blocos em
``state/indicadores/last_indicadores.json``::

    {
      "ok": true,                      # cepea.ok AND datagro.ok
      "data_referencia": "2026-10-17", # dia LOCAL do envio (gating do catch-up)
      "started_at": "...", "finished_at": "...",
      "error": "cepea: ...; datagro: ...",   # só quando alguma fonte falhou
      "cepea":   {"ok": true, "indicador": "CEPEA_BOI_GORDO", "data_indicador": ..., "valor_rs": ..., "inserted": 0, "updated": 1},
      "datagro": {"ok": true, "indicador": "DATAGRO_BOI_GORDO_SP", "datas": [...5], "valores_rs": [...5],
                  "data_indicador": ..., "valor_rs": ..., "inserted": 1, "updated": 4, "quadro": "ok"}
    }

Política "uma fonte fora do ar não derruba a outra": as duas sempre tentam; o
``ok`` global só fecha com as duas OK, então o catch-up (a cada 30 min) retenta
enquanto faltar alguma. Para não refazer a que já deu certo, o catch-up reaproveita
o bloco OK do MESMO dia (``reaproveitado: true``) e refaz só a que falhou; o
disparo manual (``indicadores-once``) usa ``force=True`` e refaz as duas.

Fonte desligada por config (``indicadores_datagro_enabled = false``) entra como
``{"ok": true, "skipped": "desligado"}`` e não pesa no ``ok`` global.

:func:`run_indicadores` NUNCA levanta. Um :class:`threading.Lock` module-level
serializa cron + catch-up + disparo manual (um envio por vez).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from .. import timewin
from . import cepea, datagro

FONTES = ("cepea", "datagro")

# Serializa cron + catch-up + disparo manual: um envio por vez.
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_status(paths) -> Optional[dict]:
    """Lê state/indicadores/last_indicadores.json (ou None se ausente/ilegível)."""
    try:
        with open(paths.indicadores_dir / "last_indicadores.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def indicadores_done_today(paths, settings) -> bool:
    """True se o envio de HOJE (dia LOCAL) já concluiu OK — nas DUAS fontes.

    Base do catch-up: no máximo 1 sucesso/dia por instância. Uma fonte ainda
    pendente mantém ``ok=False`` e o catch-up volta a rodar (só a pendente).
    """
    st = read_status(paths)
    return bool(st and st.get("ok") and st.get("data_referencia") == timewin.local_today(settings))


def _write_status(paths, status: dict, log) -> None:
    """Grava o status do último envio em state/indicadores/last_indicadores.json."""
    try:
        paths.indicadores_dir.mkdir(parents=True, exist_ok=True)
        target = paths.indicadores_dir / "last_indicadores.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 - status é diagnóstico, nunca quebra o job
        log.warning("indicadores: não consegui gravar last_indicadores.json: %s", exc)


def _bloco_anterior_ok(anterior: Optional[dict], fonte: str, data_ref: str) -> Optional[dict]:
    """Bloco da fonte já OK no MESMO dia (para o catch-up não refazer)."""
    if not anterior or anterior.get("data_referencia") != data_ref:
        return None
    bloco = anterior.get(fonte)
    if isinstance(bloco, dict) and bloco.get("ok") and not bloco.get("skipped"):
        return bloco
    return None


def run_indicadores(settings, paths, http, log, force: bool = False) -> dict:
    """Roda CEPEA e DATAGRO, cada um isolado, e grava um status só. NUNCA levanta.

    ``force=True`` (disparo manual) refaz as duas fontes mesmo que já tenham dado
    certo hoje; sem ``force`` (cron/catch-up) a fonte OK do dia é reaproveitada.
    """
    if not _LOCK.acquire(blocking=False):
        log.warning("indicadores: já há um envio em andamento; ignorando este disparo")
        return {"ok": False, "error": "envio já em andamento", "skipped": True}

    data_ref = timewin.local_today(settings)
    status: dict = {"ok": False, "data_referencia": data_ref, "started_at": _now_iso()}
    try:
        anterior = None if force else read_status(paths)
        hoje = timewin.local_now(settings).date()

        for fonte in FONTES:
            reuso = _bloco_anterior_ok(anterior, fonte, data_ref)
            if reuso is not None:
                log.info("indicadores: %s já concluído hoje; reaproveitando", fonte.upper())
                status[fonte] = dict(reuso, reaproveitado=True)
                continue
            try:
                if fonte == "cepea":
                    status[fonte] = cepea.run_cepea(http, log)
                elif not getattr(settings, "indicadores_datagro_enabled", True):
                    log.info("indicadores: DATAGRO desligado por config; pulando")
                    status[fonte] = {"ok": True, "skipped": "desligado", "indicador": datagro.INDICADOR}
                else:
                    status[fonte] = datagro.run_datagro(http, log, hoje=hoje)
            except Exception as exc:  # noqa: BLE001 - run_* não levanta, mas por garantia
                status[fonte] = {"ok": False, "error": str(exc)}
                log.error("indicadores: %s FALHOU fora do ciclo: %s", fonte.upper(), exc)

        erros = [f"{f}: {status[f].get('error')}" for f in FONTES if not status[f].get("ok")]
        status["ok"] = not erros
        if erros:
            status["error"] = "; ".join(erros)
            log.error("indicadores: job com falha (data=%s): %s", data_ref, status["error"])
        else:
            log.info("indicadores: job concluído (data=%s): CEPEA e DATAGRO OK", data_ref)
    except Exception as exc:  # noqa: BLE001 - nunca levanta; grava status e segue
        status.update({"ok": False, "error": str(exc)})
        log.error("indicadores: FALHOU (data=%s): %s", data_ref, exc)
    finally:
        status["finished_at"] = _now_iso()
        _write_status(paths, status, log)
        _LOCK.release()
    return status
