"""Coleta do Indicador do Boi DATAGRO (praça São Paulo) e envio à API do AgroDB.

O indicador é o **Indicador do Boi DATAGRO, praça SP, em R$/@** (o mesmo que a
B3 usa para liquidar o contrato BGI desde 02/2025), enviado à API com a chave
``DATAGRO_BOI_GORDO_SP``. O AgroDB monta em cima dele as médias móveis de 3 e 5
dias que cada frigorífico usa para pagar.

Fontes públicas (sem token), verificadas em 17/10/2026:

1. :data:`BOLETIM_URL` — ``boletim_cinco.svg`` (~500 KB, quase tudo PNG
   embutido): os **últimos 5 dias úteis com cotação** por estado + "Média 5
   dias". É a fonte OFICIAL e a única que vira payload. Os ``<text>`` saem na
   ordem: 5 datas ``dd/Mmm`` (sem ano!), um ``Média|5 dias`` (ignorado), e por
   estado a sigla seguida dos 5 valores e da média — ``SP|378,50|…|380,42|379,29``.
   Como são 5 datas por coleta, uma noite sem rodar não deixa buraco.
2. :data:`BOI_URL` — ``boi.svg`` (~600 KB): valor do dia por estado + a data
   ``dd/Mmm/yyyy``. A ordem dos estados NÃO é identificável pelo texto, então
   usamos SÓ a data (com ano) para conferir o ano inferido do boletim.
3. :data:`QUADRO_URL` — JSON ``quadros`` com ``{"dia": "2026-10-16", "ult":
   "380.42"}`` do ativo ``D_PEPR_SP_BR``; o ``cliente`` é o id que o próprio
   site público usa. Serve só de CONFIRMAÇÃO do dia mais recente: divergência
   vira warning (o boletim é o oficial e vai mesmo assim); falha de rede/401
   também não bloqueia.

**Regra do ano** (o boletim não traz): ano corrente; se o mês da data é MAIOR
que o mês de hoje, é virada de ano -> ano anterior (boletim de 02/Jan mostra
29/Dez, 30/Dez do ano passado). Datas têm que ser estritamente crescentes e
nenhuma pode ser depois de hoje. Se o ``boi.svg`` trouxer a mesma data (dia e
mês) com ano DIFERENTE do inferido, a inferência está errada e o job falha
fechado — melhor noite sem dado do que dado na data errada.

Validação anti-lixo (:mod:`.comum`): cada valor entre R$ 100 e R$ 1000/@, e a
"Média 5 dias" do SVG tem que bater com a média dos 5 valores lidos (tolerância
0,05): é o que garante que pegamos a LINHA de SP e não a do estado seguinte.
Qualquer desvio é "formato mudou": :class:`IndicadoresError`, nada é enviado.

Rede: ``requests.get`` em sessão NOVA (nunca o ``app.http`` autenticado — ele
carrega ``X-API-Key``/``Authorization`` do AgroDB), User-Agent de browser e
timeout (10, 60): os SVGs são grandes para a internet rural.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import requests

from .comum import VALOR_MAX_RS, VALOR_MIN_RS, IndicadoresError, conferir_resposta, parse_valor_brl

BOLETIM_URL = "https://pec.datagro.com/pec/mapas/boletim_cinco.svg"
BOI_URL = "https://pec.datagro.com/pec/mapas/boi.svg"
# `cliente` é o id público embutido no JS do site indicadordoboi.com.br.
QUADRO_URL = (
    "https://precos.api.datagro.com/quadros/?ativos=D_PEPR_SP_BR&idioma=pt-br"
    "&cliente=a81574eb-06c1-11ed-bcba-02ebefa3b928"
)
QUADRO_ATIVO = "D_PEPR_SP_BR"

# Chave do indicador na API do AgroDB (upsert por INDICADOR + DATA).
INDICADOR = "DATAGRO_BOI_GORDO_SP"

API_PATH = "/api/integracoes/indicadores"

# Quantas colunas de data o boletim traz (é o "boletim cinco").
N_DIAS = 5

# A média exibida precisa bater com a média dos 5 valores lidos (arredondamento
# de 2 casas -> no máximo 0,005 de desvio; 0,05 dá folga sem aceitar outra linha).
_TOLERANCIA_MEDIA = 0.05
# Tolerância do confronto boletim x quadro JSON (ambos com 2 casas).
_TOLERANCIA_QUADRO = 0.01

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# (connect, read): os SVGs têm ~0,5 MB e a internet rural é lenta.
_FETCH_TIMEOUT = (10, 60)

_MESES = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

_TEXT_RE = re.compile(r"<text\b[^>]*>(.*?)</text>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATA_CURTA_RE = re.compile(r"^(\d{2})/([A-Za-z]{3})$")
_DATA_LONGA_RE = re.compile(r"^(\d{2})/([A-Za-z]{3})/(\d{4})$")
_VALOR_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")


def _text_nodes(svg_text: str) -> list:
    """Conteúdo de cada ``<text>`` do SVG, sem tags internas (``<tspan>``) e com
    whitespace colapsado, na ordem do documento."""
    out = []
    for raw in _TEXT_RE.findall(svg_text or ""):
        out.append(" ".join(_TAG_RE.sub(" ", raw).split()))
    return out


def _mes(abrev: str) -> int:
    try:
        return _MESES[abrev.lower()]
    except KeyError:
        raise IndicadoresError(f"mês desconhecido no boletim DATAGRO: {abrev!r}") from None


def inferir_data(dia: int, mes: int, hoje: date) -> date:
    """``dd/Mmm`` -> ``date`` com o ano inferido: corrente, ou anterior se o mês
    está à frente do mês de hoje (virada de ano)."""
    ano = hoje.year - 1 if mes > hoje.month else hoje.year
    try:
        return date(ano, mes, dia)
    except ValueError as exc:
        raise IndicadoresError(f"data inválida no boletim DATAGRO: {dia:02d}/{mes:02d}/{ano}") from exc


def _valor(texto: str, contexto: str) -> float:
    if not _VALOR_RE.match(texto):
        raise IndicadoresError(
            f"boletim DATAGRO: esperava valor R$ em {contexto}, veio {texto!r} (formato mudou?)"
        )
    valor = parse_valor_brl(texto)
    if not (VALOR_MIN_RS <= valor <= VALOR_MAX_RS):
        raise IndicadoresError(
            f"boletim DATAGRO: valor R$ {valor:.2f}/@ ({contexto}) fora da faixa plausível "
            f"[{VALOR_MIN_RS:.0f}, {VALOR_MAX_RS:.0f}] — página mudou? NÃO enviando."
        )
    return valor


def parse_boletim_cinco(svg_text: str, hoje: date) -> list:
    """Extrai os 5 dias de SP do ``boletim_cinco.svg``.

    Função PURA (recebe o texto do SVG e a data de hoje). Devolve
    ``[{"data": "YYYY-MM-DD", "valor_rs": float}, ...]`` com exatamente
    :data:`N_DIAS` itens, em ordem crescente de data. Levanta
    :class:`IndicadoresError` se o formato mudou: menos de 5 datas, SP ausente,
    valor fora da faixa, datas não crescentes ou depois de hoje, média do SVG
    diferente da média dos valores lidos.
    """
    nodes = _text_nodes(svg_text)

    datas: list = []
    for txt in nodes:
        m = _DATA_CURTA_RE.match(txt)
        if m:
            datas.append(inferir_data(int(m.group(1)), _mes(m.group(2)), hoje))
        elif datas:
            break  # as datas vêm em bloco contíguo; o primeiro não-data encerra
    if len(datas) < N_DIAS:
        raise IndicadoresError(
            f"boletim DATAGRO com {len(datas)} data(s) dd/Mmm (esperava {N_DIAS}) — formato mudou?"
        )
    datas = datas[:N_DIAS]
    for ant, atual in zip(datas, datas[1:]):
        if atual <= ant:
            raise IndicadoresError(
                f"boletim DATAGRO com datas fora de ordem: {ant.isoformat()} -> {atual.isoformat()}"
            )
    if datas[-1] > hoje:
        raise IndicadoresError(
            f"boletim DATAGRO com data no futuro ({datas[-1].isoformat()} > hoje {hoje.isoformat()}) "
            "— relógio da máquina ou inferência do ano errada"
        )

    try:
        i_sp = nodes.index("SP")
    except ValueError:
        raise IndicadoresError("boletim DATAGRO sem a linha de SP — formato mudou?") from None
    bloco = nodes[i_sp + 1: i_sp + 1 + N_DIAS + 1]
    if len(bloco) < N_DIAS + 1:
        raise IndicadoresError("boletim DATAGRO: linha de SP truncada — formato mudou?")
    valores = [_valor(t, f"SP coluna {k + 1}") for k, t in enumerate(bloco[:N_DIAS])]
    media_svg = _valor(bloco[N_DIAS], "SP média")
    media_calc = sum(valores) / N_DIAS
    if abs(media_svg - media_calc) > _TOLERANCIA_MEDIA:
        raise IndicadoresError(
            f"boletim DATAGRO: média exibida R$ {media_svg:.2f} ≠ média dos 5 valores "
            f"R$ {media_calc:.2f} — linha de SP desalinhada? NÃO enviando."
        )

    return [{"data": d.isoformat(), "valor_rs": v} for d, v in zip(datas, valores)]


def parse_boi_data(svg_text: str) -> Optional[date]:
    """Data ``dd/Mmm/yyyy`` do ``boi.svg`` (ou ``None`` se não houver/for ilegível).

    Só a data interessa: a ordem dos estados nesse SVG não é identificável pelo
    texto, então nenhum valor dele é usado.
    """
    for txt in _text_nodes(svg_text):
        m = _DATA_LONGA_RE.match(txt)
        if not m:
            continue
        try:
            return date(int(m.group(3)), _mes(m.group(2)), int(m.group(1)))
        except (ValueError, IndicadoresError):
            return None
    return None


def parse_quadro_sp(payload) -> Optional[dict]:
    """``{"data": "YYYY-MM-DD", "valor_rs": float}`` do ativo SP no JSON ``quadros``,
    ou ``None`` se o JSON não tem o que esperamos (é só confirmação, nunca bloqueia)."""
    try:
        ativos = payload.get("ativos") or []
        for ativo in ativos:
            if ativo.get("cod") != QUADRO_ATIVO:
                continue
            dados = ativo.get("dados") or {}
            dia = datetime.strptime(str(dados.get("dia")), "%Y-%m-%d").date()
            valor = float(dados.get("ult"))
            return {"data": dia.isoformat(), "valor_rs": valor}
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _get(url: str, log, nome: str) -> requests.Response:
    """GET com sessão NOVA (nunca o ``app.http`` autenticado) e UA de browser.
    Non-2xx -> :class:`IndicadoresError`; erro de rede propaga ``RequestException``."""
    log.info("indicadores: GET DATAGRO %s", nome)
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        timeout=_FETCH_TIMEOUT,
    )
    if not (200 <= resp.status_code < 300):
        raise IndicadoresError(f"DATAGRO {nome} respondeu status {resp.status_code}")
    return resp


def fetch_boletim(log) -> str:
    return _get(BOLETIM_URL, log, "boletim_cinco.svg").text


def fetch_boi(log) -> str:
    return _get(BOI_URL, log, "boi.svg").text


def fetch_quadro(log) -> dict:
    return _get(QUADRO_URL, log, "quadros JSON").json()


def send_datagro(http, itens: list) -> dict:
    """UM POST com até 5 itens ``DATAGRO_BOI_GORDO_SP`` (``FONTE`` = URL do boletim).

    A API faz upsert por (INDICADOR, DATA): os 4 dias anteriores já existem na
    maioria das noites e voltam como ``updated`` — sucesso normal. Item recusado
    vem em ``errors`` com status 200 — :func:`comum.conferir_resposta` trata.
    """
    payload = {
        "indicadores": [
            {
                "INDICADOR": INDICADOR,
                "DATA": item["data"],
                "VALOR_RS": item["valor_rs"],
                "FONTE": BOLETIM_URL,
            }
            for item in itens
        ]
    }
    resp = http.request("POST", API_PATH, json=payload)
    return resp.json() if resp.content else {}


def _conferir_ano(itens: list, data_boi: Optional[date], log) -> None:
    """``boi.svg`` tem a data com ano: se traz o mesmo dia/mês de alguma coluna do
    boletim com ano diferente, a inferência do ano errou -> falha fechada."""
    if data_boi is None:
        log.warning("indicadores: DATAGRO boi.svg sem data legível; ano do boletim não conferido")
        return
    for item in itens:
        d = date.fromisoformat(item["data"])
        if (d.day, d.month) == (data_boi.day, data_boi.month):
            if d.year != data_boi.year:
                raise IndicadoresError(
                    f"ano inferido do boletim ({d.isoformat()}) diverge do boi.svg "
                    f"({data_boi.isoformat()}) — NÃO enviando."
                )
            log.info("indicadores: DATAGRO ano conferido pelo boi.svg (%s)", data_boi.isoformat())
            return
    log.warning("indicadores: DATAGRO boi.svg está em %s, fora das 5 datas do boletim "
                "(%s..%s); ano não conferido", data_boi.isoformat(),
                itens[0]["data"], itens[-1]["data"])


def _conferir_quadro(itens: list, quadro: Optional[dict], log) -> str:
    """Confronta a última coluna de SP com o JSON ``quadros``. Devolve o rótulo
    gravado no status: ``ok`` / ``divergente`` / ``indisponivel``."""
    if quadro is None:
        log.warning("indicadores: DATAGRO quadros JSON indisponível; seguindo só com o boletim")
        return "indisponivel"
    ultimo = itens[-1]
    if quadro["data"] != ultimo["data"] or abs(quadro["valor_rs"] - ultimo["valor_rs"]) > _TOLERANCIA_QUADRO:
        log.warning(
            "indicadores: DATAGRO quadros JSON (%s = R$ %.2f) ≠ última coluna do boletim "
            "(%s = R$ %.2f); enviando o boletim (oficial) mesmo assim",
            quadro["data"], quadro["valor_rs"], ultimo["data"], ultimo["valor_rs"],
        )
        return "divergente"
    log.info("indicadores: DATAGRO quadros JSON confirma %s = R$ %.2f/@",
             ultimo["data"], ultimo["valor_rs"])
    return "ok"


def run_datagro(http, log, hoje: Optional[date] = None) -> dict:
    """Ciclo da fonte DATAGRO: boletim -> parse -> conferências -> POST.

    Devolve o BLOCO ``datagro`` do status (``ok``, ``indicador``, ``datas``,
    ``valores_rs``, ``inserted``/``updated``, ``quadro``), nunca levanta: quem
    orquestra (:mod:`syncronizer.indicadores.job`) decide o ``ok`` global.
    """
    hoje = hoje or date.today()
    bloco: dict = {"ok": False, "indicador": INDICADOR, "fonte": BOLETIM_URL}
    try:
        itens = parse_boletim_cinco(fetch_boletim(log), hoje)
        log.info("indicadores: DATAGRO boi SP %s..%s = %s",
                 itens[0]["data"], itens[-1]["data"],
                 ", ".join(f"R$ {i['valor_rs']:.2f}" for i in itens))

        # Conferência do ano (boi.svg): rede fora não bloqueia; ano divergente bloqueia.
        try:
            data_boi = parse_boi_data(fetch_boi(log))
        except (requests.RequestException, IndicadoresError) as exc:
            log.warning("indicadores: DATAGRO boi.svg falhou (%s); ano não conferido", exc)
            data_boi = None
        _conferir_ano(itens, data_boi, log)

        # Confirmação do dia (quadros JSON): só avisa.
        try:
            quadro = parse_quadro_sp(fetch_quadro(log))
        except (requests.RequestException, IndicadoresError, ValueError) as exc:
            log.warning("indicadores: DATAGRO quadros JSON falhou (%s)", exc)
            quadro = None
        bloco["quadro"] = _conferir_quadro(itens, quadro, log)

        record = conferir_resposta(send_datagro(http, itens) or {}, len(itens), "DATAGRO", log)
        bloco.update({
            "ok": True,
            "datas": [i["data"] for i in itens],
            "valores_rs": [i["valor_rs"] for i in itens],
            "data_indicador": itens[-1]["data"],
            "valor_rs": itens[-1]["valor_rs"],
            "inserted": record.get("inserted"),
            "updated": record.get("updated"),
        })
        log.info("indicadores: DATAGRO concluído (%d itens, último %s = R$ %.2f/@, "
                 "inserted=%s updated=%s)", len(itens), itens[-1]["data"],
                 itens[-1]["valor_rs"], record.get("inserted"), record.get("updated"))
    except Exception as exc:  # noqa: BLE001 - nunca levanta; o bloco carrega o erro
        bloco.update({"ok": False, "error": str(exc)})
        log.error("indicadores: DATAGRO FALHOU: %s", exc)
    return bloco
