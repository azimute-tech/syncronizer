"""Peças comuns às fontes de indicadores: erro esperado, faixa anti-lixo, parse
de valor em formato brasileiro e conferência da resposta da API do AgroDB.

Validação anti-lixo: todo valor precisa estar entre R$ 100 e R$ 1000 por arroba
(:data:`VALOR_MIN_RS`/:data:`VALOR_MAX_RS`). Fora disso assumimos que a página
da fonte mudou de formato (ou pegamos a célula errada): erro logado, NADA é
enviado (``ok=False`` no status).
"""
from __future__ import annotations

# Faixa plausível do boi gordo em R$/@ — fora dela o formato da página mudou
# (ou pegamos a célula errada) e o valor NÃO pode ser enviado.
VALOR_MIN_RS = 100.0
VALOR_MAX_RS = 1000.0


class IndicadoresError(RuntimeError):
    """Falha esperada do job (fonte fora do ar, formato mudou, valor implausível)."""


def parse_valor_brl(texto: str) -> float:
    """``"1.048,55"`` -> ``1048.55`` (formato brasileiro: ponto milhar, vírgula decimal)."""
    return float(texto.replace(".", "").replace(",", "."))


def conferir_resposta(confirmed: dict, n_itens: int, rotulo: str, log) -> dict:
    """Lê a resposta da API (``{inserted, updated, errors[], avisos[]}``) e falha
    se algum item NÃO foi gravado.

    A rota responde **200 mesmo rejeitando itens**: o item recusado (saneamento,
    whitelist, faixa) vai em ``errors[{index, chave, message}]`` e não entra em
    ``inserted``/``updated``. Tratar isso como sucesso deixaria o status ``ok``
    sem nenhuma linha no banco — foi exatamente o que aconteceu no primeiro
    disparo contra um AgroDB ainda sem ``DATAGRO_BOI_GORDO_SP`` na whitelist.
    Devolve o registro (``inserted``/``updated``) para o status.
    """
    record = confirmed.get("data") if isinstance(confirmed.get("data"), dict) else confirmed
    errors = record.get("errors") or []
    if errors:
        detalhe = "; ".join(
            f"[{e.get('index')}] {e.get('chave') or ''}: {e.get('message')}" for e in errors
        )
        raise IndicadoresError(
            f"API rejeitou {len(errors)} de {n_itens} item(ns) do {rotulo}: {detalhe}"
        )
    for aviso in record.get("avisos") or []:
        log.warning("indicadores: aviso da API (%s): %s", rotulo, aviso)
    inserted, updated = record.get("inserted"), record.get("updated")
    if isinstance(inserted, int) and isinstance(updated, int) and inserted + updated < n_itens:
        raise IndicadoresError(
            f"API confirmou só {inserted + updated} de {n_itens} item(ns) do {rotulo} "
            f"(inserted={inserted} updated={updated}) sem apontar erro"
        )
    return record
