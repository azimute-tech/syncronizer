"""Raspagem do Indicador do Boi Gordo CEPEA e envio à API do AgroDB.

O indicador é o **Indicador do Boi Gordo CEPEA/B3, do CEPEA-ESALQ/USP
(cepea.org.br), à vista, base SP, em R$/@** — enviado à API com a chave
``CEPEA_BOI_GORDO`` e usado pelo AgroDB como preço default da simulação de
venda.

Fonte: o widget oficial do CEPEA (:data:`CEPEA_WIDGET_URL`), que devolve ~2 KB
de ``document.write(...)`` com uma tabela HTML simples; as células saem com a
regex ``<t[dh][^>]*>(.*?)</t[dh]>`` — sem dependência de parser HTML. O GET usa
User-Agent de browser (o CEPEA rejeita clients "vazios") e uma sessão NOVA do
``requests`` — nunca a sessão compartilhada do ``app.http``, que carrega
``X-API-Key``/``Authorization`` do AgroDB e não pode vazar para terceiros.
Fallback documentado caso o widget mude/saia do ar: a página
``https://cepea.org.br/br/indicador/boi-gordo.aspx`` (tabela do mesmo
indicador); trocar a fonte exige ajustar apenas ``fetch_widget``/``parse_widget``.

Validação anti-lixo: o valor precisa estar entre R$ 100 e R$ 1000 por arroba
(:data:`VALOR_MIN_RS`/:data:`VALOR_MAX_RS`). Fora disso assumimos que a página
mudou de formato: erro logado, NADA é enviado (``ok=False`` no status).

Idempotência — cenário NORMAL, não exceção: cada fazenda roda seu próprio
syncronizer, então N instâncias raspam e enviam o MESMO indicador do dia. O
desenho é idempotente de ponta a ponta: a API faz upsert por (indicador, data)
e responder ``updated`` (re-envio) é SUCESSO normal — não é erro nem warning.
O "no máximo 1 sucesso/dia" é por instância local (gating do catch-up no
scheduler); o payload não carrega NADA específico da fazenda (o indicador é
nacional; o token de autenticação já identifica quem enviou).

``run_indicadores`` NUNCA levanta: captura tudo, loga e grava o status em
``state/indicadores/last_indicadores.json``. Um :class:`threading.Lock`
module-level serializa cron + catch-up + disparo manual (indicadores-once).
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

import requests

from .. import timewin

# Widget oficial do CEPEA para o indicador 2 (Boi Gordo CEPEA/B3). O %5B%5D é
# "[]" URL-encoded (o endpoint aceita múltiplos id_indicador[]).
CEPEA_WIDGET_URL = "https://www.cepea.org.br/br/widgetproduto.js.php?id_indicador%5B%5D=2"

# Chave do indicador na API do AgroDB (upsert por INDICADOR + DATA).
INDICADOR = "CEPEA_BOI_GORDO"

API_PATH = "/api/integracoes/indicadores"

# Faixa plausível do boi gordo em R$/@ — fora dela o formato da página mudou
# (ou pegamos a célula errada) e o valor NÃO pode ser enviado.
VALOR_MIN_RS = 100.0
VALOR_MAX_RS = 1000.0

# O CEPEA devolve 403/vazio para User-Agents de script; um UA de browser comum
# resolve (validado manualmente contra o widget real).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# (connect, read) do GET do widget — resposta é ~2 KB, mas a internet rural é lenta.
_FETCH_TIMEOUT = (10, 30)

# Serializa cron + catch-up + disparo manual: um envio por vez.
_LOCK = threading.Lock()

_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_VALOR_RE = re.compile(r"^R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})$")


class IndicadoresError(RuntimeError):
    """Falha esperada do job (widget fora do ar, formato mudou, valor implausível)."""


def _clean_cell(raw: str) -> str:
    """Remove tags internas (<span>, <br/>, <img>...) e colapsa whitespace."""
    return " ".join(_TAG_RE.sub(" ", raw).split())


def _parse_valor_brl(texto: str) -> float:
    """``"1.048,55"`` -> ``1048.55`` (formato brasileiro: ponto milhar, vírgula decimal)."""
    return float(texto.replace(".", "").replace(",", "."))


def parse_widget(html: str) -> dict:
    """Extrai ``{"data": "YYYY-MM-DD", "valor_rs": float}`` do HTML do widget.

    Função PURA (recebe o HTML, sem rede) para ser testável com fixture. Varre as
    células ``<td>/<th>`` atrás da primeira data ``dd/mm/aaaa`` e do primeiro
    valor ``R$ n,nn``; os headers ("Data", "Produto", "Valor", "Fonte: Cepea")
    não casam com nenhum dos dois padrões. Levanta :class:`IndicadoresError` se
    a estrutura mudou ou o valor está fora da faixa plausível.
    """
    cells = [_clean_cell(c) for c in _CELL_RE.findall(html or "")]
    data_txt = next((c for c in cells if _DATE_RE.match(c)), None)
    valor_match = next((m for m in map(_VALOR_RE.match, cells) if m), None)
    if not data_txt or not valor_match:
        raise IndicadoresError(
            f"widget CEPEA sem data/valor reconhecíveis (formato mudou?): células={cells!r}"
        )
    try:
        data_iso = datetime.strptime(data_txt, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise IndicadoresError(f"data inválida no widget CEPEA: {data_txt!r}") from exc
    valor = _parse_valor_brl(valor_match.group(1))
    if not (VALOR_MIN_RS <= valor <= VALOR_MAX_RS):
        raise IndicadoresError(
            f"valor R$ {valor:.2f}/@ fora da faixa plausível "
            f"[{VALOR_MIN_RS:.0f}, {VALOR_MAX_RS:.0f}] — página mudou? NÃO enviando."
        )
    return {"data": data_iso, "valor_rs": valor}


def fetch_widget(log) -> str:
    """GET do widget do CEPEA com sessão NOVA (nunca o ``app.http`` autenticado).

    ``requests.get`` direto: a sessão compartilhada carrega ``X-API-Key``/
    ``Authorization`` do AgroDB (que não podem vazar pro CEPEA) e força
    ``Content-Type: application/json``. Levanta :class:`IndicadoresError` em
    status non-2xx; erros de rede (``requests.RequestException``) propagam e
    são capturados por :func:`run_indicadores`.
    """
    log.info("indicadores: GET widget CEPEA boi gordo")
    resp = requests.get(
        CEPEA_WIDGET_URL,
        headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        timeout=_FETCH_TIMEOUT,
    )
    if not (200 <= resp.status_code < 300):
        raise IndicadoresError(f"widget CEPEA respondeu status {resp.status_code}")
    return resp.text


def send_indicador(http, parsed: dict) -> dict:
    """POST do indicador na API do AgroDB via ``app.http`` (mesmo auth das outras rotas).

    O payload é SÓ o indicador nacional — nada específico da fazenda (sem
    farm_id/nome); o token da sessão já identifica quem enviou. A API faz upsert
    por (INDICADOR, DATA): re-envio (mesmo dia, N fazendas) responde ``updated``
    e é sucesso normal.
    """
    payload = {
        "indicadores": [
            {
                "INDICADOR": INDICADOR,
                "DATA": parsed["data"],
                "VALOR_RS": parsed["valor_rs"],
            }
        ]
    }
    resp = http.request("POST", API_PATH, json=payload)
    return resp.json() if resp.content else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_status(paths) -> dict | None:
    """Lê state/indicadores/last_indicadores.json (ou None se ausente/ilegível)."""
    try:
        with open(paths.indicadores_dir / "last_indicadores.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def indicadores_done_today(paths, settings) -> bool:
    """True se o envio de HOJE (dia LOCAL) já concluiu OK nesta instância.

    Base do catch-up: evita repetir o envio quando o horário agendado já teve
    sucesso — no máximo 1 sucesso/dia POR INSTÂNCIA (cada fazenda envia o seu;
    a API absorve os N envios idênticos via upsert).
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


def run_indicadores(settings, paths, http, log) -> dict:
    """Orquestra o ciclo completo: fetch -> parse/valida -> POST. NUNCA levanta.

    Ordem: GET widget (sessão nova) -> parse + validação de faixa -> POST na API
    (``app.http`` autenticado) -> grava status. Falha em qualquer etapa vira
    ``ok=False`` no status (e o catch-up do scheduler retenta no mesmo dia).
    Re-envio idempotente (API responde ``updated`` em vez de ``inserted``) é
    sucesso normal — cenário esperado com N fazendas enviando o mesmo indicador.
    """
    if not _LOCK.acquire(blocking=False):
        log.warning("indicadores: já há um envio em andamento; ignorando este disparo")
        return {"ok": False, "error": "envio já em andamento", "skipped": True}

    data_ref = timewin.local_today(settings)
    status: dict = {"ok": False, "data_referencia": data_ref, "started_at": _now_iso()}
    try:
        html = fetch_widget(log)
        parsed = parse_widget(html)
        log.info("indicadores: CEPEA boi gordo %s = R$ %.2f/@",
                 parsed["data"], parsed["valor_rs"])
        confirmed = send_indicador(http, parsed) or {}
        record = confirmed.get("data") if isinstance(confirmed.get("data"), dict) else confirmed
        status.update({
            "ok": True,
            "indicador": INDICADOR,
            "data_indicador": parsed["data"],
            "valor_rs": parsed["valor_rs"],
            "inserted": record.get("inserted"),
            "updated": record.get("updated"),
            "finished_at": _now_iso(),
        })
        log.info("indicadores: concluído (data_indicador=%s, valor=R$ %.2f/@, "
                 "inserted=%s updated=%s)", parsed["data"], parsed["valor_rs"],
                 record.get("inserted"), record.get("updated"))
    except Exception as exc:  # noqa: BLE001 - nunca levanta; grava status e segue
        status.update({"ok": False, "error": str(exc), "finished_at": _now_iso()})
        log.error("indicadores: FALHOU (data=%s): %s", data_ref, exc)
    finally:
        _write_status(paths, status, log)
        _LOCK.release()
    return status
