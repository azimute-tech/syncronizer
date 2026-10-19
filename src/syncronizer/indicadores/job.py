"""Orquestrador do job noturno de indicadores: fonte DATAGRO, um status só.

A fonte roda em :func:`datagro.run_datagro` (que nunca levanta) e devolve um
bloco; este módulo grava ``state/indicadores/last_indicadores.json``::

    {
      "ok": true,                      # = datagro.ok
      "data_referencia": "2026-10-17", # dia LOCAL do envio (gating do catch-up)
      "started_at": "...", "finished_at": "...",
      "error": "datagro: ...",         # só quando a fonte falhou
      "datagro": {"ok": true, "indicador": "DATAGRO_BOI_GORDO_SP", "datas": [...5], "valores_rs": [...5],
                  "data_indicador": ..., "valor_rs": ..., "inserted": 1, "updated": 4, "quadro": "ok"}
    }

O catch-up (a cada 30 min) retenta enquanto o bloco ``datagro`` de hoje não
estiver OK. Se ele já deu certo no MESMO dia, o bloco é reaproveitado
(``reaproveitado: true``) em vez de refeito; o disparo manual
(``indicadores-once``) usa ``force=True`` e refaz.

Status gravado por versão anterior do serviço pode trazer blocos de fontes que
saíram do job (ver CHANGELOG): são ignorados na leitura — só o bloco
``datagro`` decide se o dia está feito — e somem na próxima gravação.

Idempotência — cenário NORMAL, não exceção: cada fazenda roda seu próprio
syncronizer, então N instâncias coletam e enviam o MESMO indicador do dia. A
API faz upsert por (indicador, data) e responder ``updated`` (re-envio) é
SUCESSO normal — não é erro nem warning. O "no máximo 1 sucesso/dia" é por
instância local (gating do catch-up no scheduler); o payload não carrega NADA
específico da fazenda (o indicador é nacional; o token já identifica quem enviou).

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
from . import datagro

FONTE = "datagro"

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


def _bloco_ok_do_dia(st: Optional[dict], data_ref: str) -> Optional[dict]:
    """Bloco ``datagro`` já OK no dia ``data_ref`` (ou None).

    Olha o BLOCO, não o ``ok`` do topo: num status antigo o topo pode estar
    ``false`` só por causa de uma fonte que saiu do job, e um status sem o bloco
    ``datagro`` (versão bem antiga) não conta como feito. Bloco ``skipped`` (fonte
    desligada por uma config que não existe mais) também não conta: a coleta
    volta no mesmo dia em que o serviço atualiza.
    """
    if not isinstance(st, dict) or st.get("data_referencia") != data_ref:
        return None
    bloco = st.get(FONTE)
    if isinstance(bloco, dict) and bloco.get("ok") and not bloco.get("skipped"):
        return bloco
    return None


def indicadores_done_today(paths, settings) -> bool:
    """True se o envio de HOJE (dia LOCAL) do DATAGRO já concluiu OK.

    Base do catch-up: no máximo 1 sucesso/dia por instância.
    """
    return _bloco_ok_do_dia(read_status(paths), timewin.local_today(settings)) is not None


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


def run_indicadores(settings, paths, http, log, force: bool = False) -> dict:
    """Roda o DATAGRO e grava o status. NUNCA levanta.

    ``force=True`` (disparo manual) refaz a coleta mesmo que já tenha dado certo
    hoje; sem ``force`` (cron/catch-up) o bloco OK do dia é reaproveitado.
    """
    if not _LOCK.acquire(blocking=False):
        log.warning("indicadores: já há um envio em andamento; ignorando este disparo")
        return {"ok": False, "error": "envio já em andamento", "skipped": True}

    data_ref = timewin.local_today(settings)
    status: dict = {"ok": False, "data_referencia": data_ref, "started_at": _now_iso()}
    try:
        reuso = None if force else _bloco_ok_do_dia(read_status(paths), data_ref)
        if reuso is not None:
            log.info("indicadores: DATAGRO já concluído hoje; reaproveitando")
            status[FONTE] = dict(reuso, reaproveitado=True)
        else:
            try:
                hoje = timewin.local_now(settings).date()
                status[FONTE] = datagro.run_datagro(http, log, hoje=hoje)
            except Exception as exc:  # noqa: BLE001 - run_datagro não levanta, mas por garantia
                status[FONTE] = {"ok": False, "error": str(exc)}
                log.error("indicadores: DATAGRO FALHOU fora do ciclo: %s", exc)

        status["ok"] = bool(status[FONTE].get("ok"))
        if status["ok"]:
            log.info("indicadores: job concluído (data=%s): DATAGRO OK", data_ref)
        else:
            status["error"] = f"{FONTE}: {status[FONTE].get('error')}"
            log.error("indicadores: job com falha (data=%s): %s", data_ref, status["error"])
    except Exception as exc:  # noqa: BLE001 - nunca levanta; grava status e segue
        status.update({"ok": False, "error": str(exc)})
        log.error("indicadores: FALHOU (data=%s): %s", data_ref, exc)
    finally:
        status["finished_at"] = _now_iso()
        _write_status(paths, status, log)
        _LOCK.release()
    return status
