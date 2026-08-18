"""TGC `lotes_saida_custos` endpoint — rateio financeiro do abate (DET_LOTESAIDA_FIN).

Source: Firebird 2.5 `DET_LOTESAIDA_FIN` (576 linhas na base de staging).
Target: POST /api/integracoes/tgc/lotes-saida-custos  body {"custos": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda logo depois de `lotes_saida` (order=62): o lote que a rubrica aponta
(`DLSF_CODLOTESAIDA` -> `lotes_saida_tgc.cod_lote_saida`; zero órfãos conferidos na
staging) já foi enviado no mesmo ciclo.

USO: RECONCILIAÇÃO do fechamento — é o rateio que o PRÓPRIO TGC fez, nunca fonte
canônica. A receita e os custos canônicos do abate são do AgroDB; este espelho serve
para confrontar os dois e mostrar a divergência.

A tabela é um TEMPLATE de 12 rubricas por lote (12 x 48 = 576 linhas exatas): BONIFICACOES
COURO, COMMISSAO VENDA, CREDITO ICMS, CREDITO ICMS FRETE, FUNRURAL, VENDA ANIMAIS e
mais 6. Na staging só a rubrica VENDA ANIMAIS está preenchida — 42 linhas com valor,
534 com 0,00 (e `DLSF_PORC_VALOR` 0,00 nas mesmas 534).

FULL SCAN + row_hash — 576 linhas, e o rateio é justamente o que o operador corrige
depois de fechar a nota do frigorífico; não existe `DLSF_DATA_UPDATE` para watermark, e
um watermark por `DLSF_CODIGO` congelaria as rubricas no estado em que o template
nasceu (zeradas). Mesmo critério de `lotes_saida`. ``reconcile_deletes`` fica False
como nos demais feeds.

Zeros: `DLSF_VALOR` é R$, então o 0,00 do template não preenchido é "não informado" e
vira None pela regra geral do contrato ("nunca plantar custo/valor falso") — mandar
534 rubricas de R$ 0,00 faria a reconciliação afirmar que não houve comissão, ICMS nem
FUNRURAL, quando o que houve foi ausência de lançamento. `DLSF_PORC_VALOR` NÃO é R$ e
segue como número (0.0 inclusive): é o percentual do rateio, e o destino já enxerga a
ausência pelo VALOR nulo ao lado.

`DLSF_STATUS` é CHAR(1) na origem, não texto livre: 'R' (384 linhas) e 'D' (192).
`DLSF_FLAG_IMPOSTO` é o 'S'/'N' clássico (336 'S', 240 'N') e vira boolean.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    flag_sn,
    num,
    opt_str,
    req_str,
)


def _valor(value):
    """Valor da rubrica em R$, ou None para o 0,00 do template não preenchido.

    534 das 576 linhas da staging são rubricas do template que ninguém lançou; mandá-las
    como R$ 0,00 afirmaria "não houve esse custo" em vez de "não foi informado".
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class LotesSaidaCustosEndpoint(BatchEndpoint):
    name = "lotes_saida_custos"      # -> control table ep_lotes_saida_custos
    primary_key = "COD_CUSTO"        # PK real da tabela (DLSF_CODIGO)
    order = 62
    api_path = "/api/integracoes/tgc/lotes-saida-custos"  # URL com hifen (padrao de rota do AgroDB)
    api_method = "POST"

    # Full scan + row_hash: o rateio é corrigido depois de lançado e a origem não tem
    # coluna de watermark — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "custos"
    record_key = "COD_CUSTO"
    error_key = "cod_custo"

    _BASE_SQL = """
        SELECT
            c.DLSF_CODIGO       AS COD_CUSTO,
            c.DLSF_CODLOTESAIDA AS COD_LOTE_SAIDA,
            c.DLSF_DESCRICAO    AS DESCRICAO,
            c.DLSF_VALOR        AS VALOR,
            c.DLSF_STATUS       AS STATUS,
            c.DLSF_PORC_VALOR   AS PORC_VALOR,
            c.DLSF_FLAG_IMPOSTO AS IMPOSTO
        FROM DET_LOTESAIDA_FIN c
        ORDER BY c.DLSF_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_CUSTO": req_str(row.get("COD_CUSTO")),
            "COD_LOTE_SAIDA": req_str(row.get("COD_LOTE_SAIDA")),
            "DESCRICAO": opt_str(row.get("DESCRICAO")),
            # R$ 0,00 do template não lançado = "não informado" -> None (ver _valor)
            "VALOR": _valor(row.get("VALOR")),
            "STATUS": opt_str(row.get("STATUS")),          # 'R' | 'D' (CHAR(1) na origem)
            # percentual, não R$: 0.0 viaja como está
            "PORC_VALOR": num(row.get("PORC_VALOR")),
            "IMPOSTO": flag_sn(row.get("IMPOSTO")),
        }
