"""Aviso de fim de ciclo para a API: ``POST /api/integracoes/tgc/ciclo-concluido``.

Ao fim de cada ciclo que leu o Firebird, o AgroDB precisa saber que a carga
terminou para recalcular os derivados da fazenda (uma vez por ciclo, e não a cada
lote recebido). Contrato:

    body     {ciclo_id, iniciado_em, concluido_em, recursos: {<endpoint>: {enviados, falhas}}}
    resposta {versao, recalculado}

Regras:

- **Sempre avisa**, mesmo num ciclo sem mudança: é um POST pequeno a cada ciclo, e
  deixa a API recalcular uma fazenda marcada como pendente (ex.: dado que chegou num
  ciclo cujo aviso se perdeu). Quem decide se recalcula é a API (``recalculado``).
- **Repete até confirmar.** O aviso é gravado no ``control.db`` (``_sync_meta``)
  ANTES da primeira tentativa; algumas tentativas rápidas com espera crescente dentro
  do ciclo e, se ainda falhar (rede, 5xx, 408, 429), nova tentativa no início e no fim
  dos próximos ciclos até chegar um 2xx. Sobrevive a reinício do serviço.
- **Avisos pendentes se fundem** num só: o ciclo novo substitui o pendente, mantendo
  o ``iniciado_em`` mais antigo e somando ``enviados``/``falhas`` por recurso. A API
  recalcula uma vez para tudo o que chegou desde a última confirmação, em vez de
  receber uma rajada de avisos depois de uma queda de internet.
- **404** = API antiga, sem a rota: um WARNING só, descarta o pendente e não tenta
  mais até o serviço reiniciar (a API tem a reserva "pendente há > 15 min recalcula na
  primeira leitura"). Outro 4xx (400/401/403…) não melhora repetindo: WARNING e descarta.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Dict, Optional

import requests

API_PATH = "/api/integracoes/tgc/ciclo-concluido"
META_KEY = "ciclo_concluido_pendente"
# Esperas entre as tentativas dentro do ciclo (s): 3 tentativas no total. O HttpClient
# já repete 5xx/rede no transporte; isto cobre a falha que sobra depois dele.
BACKOFF_SECONDS = (2.0, 5.0)
# 4xx que valem repetir (timeout do servidor, limite de taxa).
_RETRIABLE_4XX = (408, 429)


def build_payload(stats, iniciado_em: str, concluido_em: str,
                  ciclo_id: Optional[str] = None) -> dict:
    recursos: Dict[str, Dict[str, int]] = {}
    for name, est in sorted(stats.endpoints.items()):
        recursos[name] = {"enviados": int(est.sent), "falhas": int(est.failed)}
    return {
        "ciclo_id": ciclo_id or uuid.uuid4().hex,
        "iniciado_em": iniciado_em,
        "concluido_em": concluido_em,
        "recursos": recursos,
    }


def merge_payloads(old: dict, new: dict) -> dict:
    """Funde o aviso pendente com o do ciclo novo (o novo vence; contadores somam)."""
    recursos: Dict[str, Dict[str, int]] = {}
    for src in (old.get("recursos") or {}, new.get("recursos") or {}):
        for name, c in src.items():
            acc = recursos.setdefault(name, {"enviados": 0, "falhas": 0})
            acc["enviados"] += int(c.get("enviados", 0) or 0)
            acc["falhas"] += int(c.get("falhas", 0) or 0)
    # ISO-8601 em UTC com o mesmo formato (now_iso) ordena como texto.
    inicios = [x for x in (old.get("iniciado_em"), new.get("iniciado_em")) if x]
    iniciado = min(inicios) if inicios else None
    return {
        "ciclo_id": new["ciclo_id"],
        "iniciado_em": iniciado,
        "concluido_em": new.get("concluido_em"),
        "recursos": dict(sorted(recursos.items())),
    }


class CicloNotifier:
    def __init__(self, store, http, log,
                 sleep: Callable[[float], None] = time.sleep,
                 backoff=BACKOFF_SECONDS):
        self.store = store
        self.http = http
        self.log = log
        self._sleep = sleep
        self._backoff = tuple(backoff)
        self.unsupported = False  # 404 visto: rota ausente até o próximo reinício

    # -- estado persistido -------------------------------------------------
    def pending(self) -> Optional[dict]:
        raw = self.store.get_meta(META_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            self.log.warning("ciclo-concluido: aviso pendente ilegível no control.db; descartado")
            self._clear()
            return None
        return data if isinstance(data, dict) and data.get("ciclo_id") else None

    def _save(self, payload: dict) -> None:
        self.store.set_meta(META_KEY, json.dumps(payload, sort_keys=True))

    def _clear(self) -> None:
        self.store.set_meta(META_KEY, "")

    # -- API pública -------------------------------------------------------
    def notify(self, payload: dict) -> bool:
        """Registra o aviso do ciclo (fundido com um pendente) e tenta entregar."""
        if self.unsupported:
            return False
        old = self.pending()
        self._save(merge_payloads(old, payload) if old else payload)
        return self.flush(retries=True)

    def flush(self, retries: bool = False) -> bool:
        """Tenta entregar o pendente, se houver. True = nada pendente ao final."""
        if self.unsupported:
            return False
        payload = self.pending()
        if payload is None:
            return True
        attempts = 1 + (len(self._backoff) if retries else 0)
        for i in range(attempts):
            outcome = self._post(payload)
            if outcome not in ("retry", "defer"):
                return outcome == "ok"
            if outcome == "defer":
                break  # timeout: não segura o ciclo; tenta de novo no próximo
            if i < attempts - 1:
                self._sleep(self._backoff[i])
        self.log.warning(
            "ciclo-concluido: não confirmado (ciclo_id=%s); fica pendente e será "
            "reenviado no próximo ciclo", payload["ciclo_id"])
        return False

    # -- internos ----------------------------------------------------------
    def _post(self, payload: dict) -> str:
        """'ok' | 'drop' (pendente já tratado) | 'retry' | 'defer' (timeout: só no próximo ciclo)."""
        try:
            resp = self.http.request("POST", API_PATH, json=payload)
        except requests.Timeout as exc:
            # o HttpClient já repetiu no transporte; repetir aqui prenderia o ciclo
            # por minutos (timeout x tentativas) com a API lenta.
            self.log.warning("ciclo-concluido: tempo esgotado: %s", exc)
            return "defer"
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 404:
                self.unsupported = True
                self._clear()
                self.log.warning(
                    "ciclo-concluido: a API respondeu 404 (versão sem a rota %s); "
                    "aviso de fim de ciclo desligado até o serviço reiniciar", API_PATH)
                return "drop"
            if status is not None and 400 <= status < 500 and status not in _RETRIABLE_4XX:
                self._clear()
                self.log.warning(
                    "ciclo-concluido: a API recusou o aviso (HTTP %s), descartado: %s",
                    status, exc)
                return "drop"
            self.log.warning("ciclo-concluido: falha HTTP %s: %s", status, exc)
            return "retry"
        except Exception as exc:  # noqa: BLE001 - rede, RetryError (5xx esgotado), timeout
            self.log.warning("ciclo-concluido: falha de envio: %s", exc)
            return "retry"

        self._clear()
        versao = recalculado = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                versao, recalculado = body.get("versao"), body.get("recalculado")
        except Exception:  # noqa: BLE001 - 2xx sem JSON ainda confirma
            pass
        self.log.info("ciclo-concluido confirmado: ciclo_id=%s versao=%s recalculado=%s",
                      payload["ciclo_id"], versao, recalculado)
        return "ok"
