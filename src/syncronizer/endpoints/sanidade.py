"""TGC `sanidade` endpoint — aplicações sanitárias (DET_SANIDADE) para o AgroDB.

Source: Firebird 2.5 `DET_SANIDADE` (0 linhas na base de staging).
Target: POST /api/integracoes/tgc/sanidade  body {"sanidade": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda no fim do bloco de fechamento (order=63): o animal que a aplicação aponta
(`DSANI_CODANIMAL`) já foi enviado no mesmo ciclo pelo feed `animais`.

A BASE DE STAGING TEM ZERO APLICAÇÕES. A estrutura e a ingestão entram agora porque a
produção do cliente tem o dado; enquanto não vier, o relatório de Sanidade não ganha
página própria — vira uma seção do Zootécnico com estado vazio honesto ("sem
aplicações sincronizadas no período") e o custo sanitário do fechamento sai como "não
informado" em vez de R$ 0. Consequência prática deste feed: enquanto a tabela estiver
vazia ele extrai 0 linhas e não faz requisição nenhuma — o que é o comportamento
correto, e NÃO deve ser confundido com feed quebrado.

INCREMENTAL por `DSANI_CODIGO` — mesma máquina de `movimentacoes`. Uma aplicação
sanitária é um FATO histórico (animal X recebeu a dose Y no dia Z): o TGC lança uma nova
linha, não edita a antiga, e a tabela não tem coluna de atualização (`DSANI_DATAREG`,
`DSANI_SYNC_DATA` — nenhum `DSANI_DATA_UPDATE`, ao contrário de CAD_FORNECIMENTO,
CAD_BATIDA e DET_PESAGEM, onde a existência da coluna denunciou a correção retroativa e
levou ao full scan). O primeiro ciclo não tem watermark e lê a tabela inteira.

Duas consequências do watermark na PK, ambas intencionais e idênticas às de
`movimentacoes`:
  * um UPDATE numa aplicação já sincronizada NÃO é capturado (o id não mudou);
  * ``reconcile_deletes`` fica False — tombstone só é válido depois de um full scan;
    a partir de um delta ele apagaria tudo que não veio no último delta, ou seja, tudo.

AVISO: com 0 linhas na staging não há como MEDIR se a premissa "append-only" se
sustenta, como foi possível medir (e refutar) em DET_PESAGEM. Quando a base de produção
chegar, vale conferir `DSANI_DATAREG` contra o comportamento real antes de confiar no
watermark; virar full scan é uma linha (``incremental_column = None``).

Zeros: `DSANI_VALOR_APLICACAO` é R$ e o 0 do TGC é "não informado" -> None, pela regra
geral do contrato. `DSANI_DOSE_ML` é dose medida (mL): 0 seria dado real e viaja como
0.0. `DSANI_CODANIMAL`, `DSANI_CODPRODUTO` e `DSANI_CODPROTOCOLO` são FKs INTEGER onde
o TGC usa 0 como "sem relacionado", então passam por ``opt_code``.

Tipos na origem: `DSANI_DOSE_ML` e `DSANI_VALOR_APLICACAO` são DOUBLE PRECISION (não
BIGINT com escala como na maioria dos feeds); `DSANI_SISBOV` é VARCHAR(20) e passa por
``clean_id`` como em `animais` — identificador destruído pelo Excel não vira chave.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    clean_id,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
    watermark_param,
)


def _valor(value):
    """Valor da aplicação em R$, ou None para o 0 de "não informado" do TGC."""
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class SanidadeEndpoint(BatchEndpoint):
    name = "sanidade"                # -> control table ep_sanidade
    primary_key = "COD_SANIDADE"     # PK real da tabela (DSANI_CODIGO)
    order = 63
    api_path = "/api/integracoes/tgc/sanidade"
    api_method = "POST"

    # Delta pela PK: o orquestrador lê esta coluna RAW de cada linha extraída para
    # avançar o watermark, e é por isso que o _BASE_SQL seleciona DSANI_CODIGO SEM
    # alias (um alias faria row.get("DSANI_CODIGO") devolver None e o watermark
    # nunca sairia do lugar) — mesmo cuidado de `movimentacoes`.
    incremental_column = "DSANI_CODIGO"
    reconcile_deletes = False        # inválido num extract incremental — ver docstring

    payload_key = "sanidade"
    record_key = "COD_SANIDADE"
    error_key = "cod_sanidade"

    _BASE_SQL = """
        SELECT
            s.DSANI_CODIGO,
            s.DSANI_CODANIMAL        AS COD_ANIMAL,
            s.DSANI_SISBOV           AS SISBOV,
            s.DSANI_DATA_APLICACAO   AS DATA_APLICACAO,
            s.DSANI_CODPRODUTO       AS COD_PRODUTO,
            s.DSANI_TIPO             AS TIPO,
            s.DSANI_MOTIVO_APLICACAO AS MOTIVO,
            s.DSANI_DOSE_ML          AS DOSE_ML,
            s.DSANI_VALOR_APLICACAO  AS VALOR_APLICACAO,
            s.DSANI_DATA_CARENCIA    AS DATA_CARENCIA,
            s.DSANI_CODPROTOCOLO     AS COD_PROTOCOLO
        FROM DET_SANIDADE s
    """
    _WHERE_INCREMENTAL = " WHERE s.DSANI_CODIGO > ?"
    _ORDER_BY = " ORDER BY s.DSANI_CODIGO"

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        if ctx.last_watermark is None:
            # primeiro ciclo (ou depois de um reset): lê o histórico inteiro uma vez
            return ExtractSpec(sql=self._BASE_SQL + self._ORDER_BY, params=())
        sql = self._BASE_SQL + self._WHERE_INCREMENTAL + self._ORDER_BY
        return ExtractSpec(
            sql=sql,
            params=(watermark_param(ctx.last_watermark),),
            incremental=True,
        )

    def transform(self, row: dict) -> dict:
        return {
            "COD_SANIDADE": req_str(row.get("DSANI_CODIGO")),
            "COD_ANIMAL": opt_code(row.get("COD_ANIMAL")),
            "SISBOV": clean_id(row.get("SISBOV")),
            "DATA_APLICACAO": iso_date(row.get("DATA_APLICACAO")),
            "COD_PRODUTO": opt_code(row.get("COD_PRODUTO")),
            "TIPO": opt_str(row.get("TIPO")),
            "MOTIVO": opt_str(row.get("MOTIVO")),
            # dose medida: 0 seria dado real e viaja como 0.0
            "DOSE_ML": num(row.get("DOSE_ML")),
            # R$ 0 = "não informado" do TGC -> None (ver _valor)
            "VALOR_APLICACAO": _valor(row.get("VALOR_APLICACAO")),
            "DATA_CARENCIA": iso_date(row.get("DATA_CARENCIA")),
            "COD_PROTOCOLO": opt_code(row.get("COD_PROTOCOLO")),
        }
