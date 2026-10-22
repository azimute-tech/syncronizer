"""TGC `insumos_baixas` endpoint — baixas avulsas de insumo (CAD_BAIXA_ALIM) para o AgroDB.

Source: Firebird 2.5 `CAD_BAIXA_ALIM`, com o nome do centro de custo de
        `CAD_CENTROCUSTO`. Na base de staging (20/10/2026): 115 baixas de 11 insumos,
        13/04 a 15/10/2026, Σ 2.799.886,55 kg — CONSUMO 108 (1.949.006 kg), PERDA 3
        (103.010,55 kg), SAIDA 4 (747.870 kg).
Target: POST /api/integracoes/tgc/insumos-baixas  body {"baixas": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

A baixa é a SAÍDA do estoque que não passa pela fábrica: consumo direto, perda,
devolução/saída para outra fazenda, zeragem de silo. No Relatório de Insumos ela entra
no SALDO (saldo = entradas − batidas − baixas, a conta da Posição do TGC) mas NÃO no
ritmo de consumo — são ajustes pontuais: a baixa 73 é um "CONSUMO" de 1.893.339 kg num
único dia (13/10) para zerar o silo "MILHO GRÃO UMIDO_T1 MENOR" (hoje encerrado:
`AA_REL_ESTOQUE_ATUAL = 'N'`). Roda depois do catálogo
`alimentos` (order=20), que dá nome ao `COD_ALIMENTO` — zero baixas órfãs, conferido
na staging —, logo depois das entradas (order=58).

FULL SCAN + row_hash — centenas de linhas, e a baixa é EDITÁVEL no TGC (a origem
mantém `CBA_DATA_UPDATE`, preenchida em 115/115) sem o `CBA_CODIGO` mudar; um
watermark por código não veria a correção. ``reconcile_deletes`` fica False como nos
demais feeds.

Regra de zero: `QTDE_KG` 0 É dado real e viaja como 0.0 (1 baixa na staging: a 27,
CONSUMO de 22/06). `TIPO` viaja como veio (CONSUMO | PERDA | SAIDA na staging).

`CENTRO_CUSTO` vem por LEFT JOIN — um centro de custo apagado não pode sumir com o
peso baixado (0 órfãos hoje; um só centro em uso, "AGROPEC VITÓRIA 2", em 115/115).

`OBS` (CBA_OBS) é BLOB SUB_TYPE TEXT em WIN1252. Medido: o driver firebirdsql devolve
o BLOB de texto já como str — o servidor translitera para o charset da conexão (UTF8,
o padrão do agente; conferido também com WIN1252) e o acento chega certo ("DEVOLUÇÃO
DE MILHO PARA FAZENDA NASCENTE"). Preenchida em 3 das 115, com quebra de linha
(CRLF) dentro do texto, que é preservada. Regra do contrato: trim, vazio -> None,
truncada em 500 caracteres (texto livre do operador num BLOB sem limite; o maior hoje
tem 83).

Escala: CBA_QTDEKG é BIGINT scale -4 e o driver devolve Decimal com a escala já
aplicada (1893339.0000 kg).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)

OBS_MAX = 500  # contrato: obs truncada em 500 caracteres na origem


def _obs(value):
    """Observação do operador: trim, vazio -> None, no máximo ``OBS_MAX`` caracteres.

    O BLOB de texto já chega como str (o ``normalize`` do agente decoda bytes); o
    ramo de bytes é só defesa para uma conexão ``charset = NONE``, em que o BLOB
    WIN1252 viria cru.
    """
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            value = raw.decode("cp1252", errors="replace")
    text = opt_str(value)
    if text is None:
        return None
    return text[:OBS_MAX].rstrip() or None


class InsumosBaixasEndpoint(BatchEndpoint):
    name = "insumos_baixas"          # -> control table ep_insumos_baixas
    primary_key = "COD_BAIXA"        # PK real da tabela (CBA_CODIGO)
    order = 58                       # depois de alimentos (20), das batidas e das entradas
    api_path = "/api/integracoes/tgc/insumos-baixas"  # URL com hífen; payload_key "baixas"
    api_method = "POST"

    # Full scan + row_hash: a baixa é editável no TGC (CBA_DATA_UPDATE) — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "baixas"
    record_key = "COD_BAIXA"
    # A API do AgroDB ecoa o identificador do item em `errors[].chave` (TgcIngestError:
    # {index, chave, message}) — é por ele que a rejeição casa com a linha.
    error_key = "chave"

    _BASE_SQL = """
        SELECT
            b.CBA_CODIGO      AS COD_BAIXA,
            b.CBA_CODALIMENTO AS COD_ALIMENTO,
            b.CBA_DATA        AS DATA,
            b.CBA_QTDEKG      AS QTDE_KG,
            b.CBA_TIPO        AS TIPO,
            cc.CCC_NOME       AS CENTRO_CUSTO,
            b.CBA_OBS         AS OBS
        FROM CAD_BAIXA_ALIM b
        LEFT JOIN CAD_CENTROCUSTO cc ON cc.CCC_CODIGO = b.CBA_CODCENTROCUSTO
        ORDER BY b.CBA_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_BAIXA": req_str(row.get("COD_BAIXA")),
            # join com alimentos_tgc.cod_alimento (AA_CODIGO)
            "COD_ALIMENTO": opt_code(row.get("COD_ALIMENTO")),
            "DATA": iso_date(row.get("DATA")),
            # kg 0 é dado real — viaja como 0.0
            "QTDE_KG": num(row.get("QTDE_KG")),
            # CONSUMO | PERDA | SAIDA, como veio
            "TIPO": opt_str(row.get("TIPO")),
            "CENTRO_CUSTO": opt_str(row.get("CENTRO_CUSTO")),
            # BLOB de texto: trim, vazio -> None, <= 500 caracteres
            "OBS": _obs(row.get("OBS")),
        }
