"""TGC `lotes_saida` endpoint — lote de saída / fechamento de abate (CAD_LOTESAIDA).

Source: Firebird 2.5 `CAD_LOTESAIDA` (48 linhas na base de staging).
Target: POST /api/integracoes/tgc/lotes-saida  body {"lotes_saida": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

O lote de saída é a UNIDADE do fechamento de abate: é ele que o animal referencia
(`CAD_ANIMAL.CA_LOTESAIDA`, enviado pelo feed `animais`) e a que os custos de
`lotes_saida_custos` se penduram. Roda no bloco de fechamento (order=61).

FULL SCAN + row_hash — 48 linhas, e a tabela é toda feita de correção: o lote nasce no
embarque com peso de confinamento e vai recebendo peso de porteira, peso de frigorífico,
carcaça, RC, nota fiscal e pagamento ao longo de semanas. Não existe coluna de
watermark que capture isso (não há `CLS_DATA_UPDATE`), e um watermark por `CLS_CODIGO`
congelaria o lote no estado do embarque. Mesmo critério dos catálogos e de
`metas_abate`. ``reconcile_deletes`` fica False como nos demais feeds.

VALORES FINANCEIROS SÃO REFERÊNCIA/CONFERÊNCIA. A receita canônica do abate é o
romaneio do AgroDB (`abates_romaneio`); quando não houver romaneio o relatório exibe o
valor do TGC MARCADO COMO FALLBACK — mesmo padrão de `custo_final`. Este feed não
decide nada disso, só espelha o que o TGC tem.

Zeros medidos na staging (regra do contrato: valor/custo 0 é "não informado" e vira
None; peso/quantidade 0 é dado real e viaja como 0.0):
  * `CLS_FRIG_PESOKG` = 0 em 48/48 -> None (o contrato já prevê: "0 é comum").
  * `CLS_VALOR_IMPOSTOS`, `CLS_VALOR_NF`, `CLS_CUSTODIARIA` = 0 em 48/48 -> None.
  * `CLS_VALORBRUTO` / `CLS_VALORLIQUIDO` = 0 em 6/48 -> None; os outros 42 têm valor.
  * `CLS_CONF_PESOKG` = 0 em 3/48 e `CLS_KGTOTALCARCACA` = 0 em 6/48: são PESOS, e o
    contrato só manda anular o peso de frigorífico, então viajam como 0.0.

DIVERGÊNCIAS medidas contra o contrato (documentadas, não corrigidas em silêncio):
  * `CLS_PORT_PESOKG` (peso na porteira do frigorífico) é 0 em 44 das 48 linhas — o
    mesmo padrão que levou o contrato a mandar anular `FRIG_PESO_KG`. Como o contrato
    NÃO estende a regra a este campo, ele viaja como 0.0; se o destino quiser ler isso
    como "não informado", a regra tem que ser decidida no contrato, não aqui.
  * `CLS_NUMERONF`, `CLS_NUMCONTRATO`, `CLS_DATAPAGTO_PREV` e `CLS_DATAPAGTO_PAGO` são
    NULL em 48/48: o bloco fiscal/financeiro do lote nunca foi preenchido nesta base.
    As colunas entram prontas para quando a fazenda passar a preencher — sem elas o
    dado novo seria descartado em silêncio (mesmo racional do VALOR_ENTRADA em
    `animais`).

Escalas: todas as colunas de peso/valor/RC são BIGINT scale -2 e o driver firebirdsql
devolve Decimal com a escala já aplicada (61612.00 kg, 53.25 %, 800598.32 R$).
`CLS_FLAG_FECHADO` é CHAR(1) 'S'/'N' (26 'S' e 22 'N' na staging) e vira boolean.
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    flag_sn,
    integer,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)


def _valor(value):
    """Valor em R$, ou None para o 0,00 que o TGC deixa quando não informado.

    Encaminhar 0 plantaria receita/custo falso de R$ 0,00 num campo que o relatório de
    fechamento usa como fallback do romaneio (ver docstring do módulo).
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class LotesSaidaEndpoint(BatchEndpoint):
    name = "lotes_saida"             # -> control table ep_lotes_saida
    primary_key = "COD_LOTE_SAIDA"   # PK real da tabela (CLS_CODIGO)
    order = 61
    api_path = "/api/integracoes/tgc/lotes-saida"  # URL com hifen (padrao de rota do AgroDB); payload_key segue com underscore
    api_method = "POST"

    # Full scan + row_hash: o lote é corrigido por semanas depois de criado e não há
    # coluna de watermark na origem — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "lotes_saida"
    record_key = "COD_LOTE_SAIDA"
    error_key = "cod_lote_saida"

    _BASE_SQL = """
        SELECT
            l.CLS_CODIGO          AS COD_LOTE_SAIDA,
            l.CLS_CODDESTINO      AS COD_DESTINO,
            l.CLS_DATAABATE       AS DATA_ABATE,
            l.CLS_DATAEMBARQUE    AS DATA_EMBARQUE,
            l.CLS_QTDECABTOTAL    AS QTD_CABECAS,
            l.CLS_CONF_PESOKG     AS CONF_PESO_KG,
            l.CLS_CONF_QTDECAB    AS CONF_QTD_CAB,
            l.CLS_PORT_PESOKG     AS PORT_PESO_KG,
            l.CLS_FRIG_PESOKG     AS FRIG_PESO_KG,
            l.CLS_FRIG_QTDECAB    AS FRIG_QTD_CAB,
            l.CLS_KGTOTALCARCACA  AS KG_TOTAL_CARCACA,
            l.CLS_TOTALARROBA     AS TOTAL_ARROBA,
            l.CLS_RC              AS RC_PCT,
            l.CLS_VALORBRUTO      AS VALOR_BRUTO,
            l.CLS_VALORLIQUIDO    AS VALOR_LIQUIDO,
            l.CLS_VALOR_IMPOSTOS  AS VALOR_IMPOSTOS,
            l.CLS_NUMERONF        AS NUMERO_NF,
            l.CLS_VALOR_NF        AS VALOR_NF,
            l.CLS_DATAPAGTO_PREV  AS DATA_PAGTO_PREV,
            l.CLS_DATAPAGTO_PAGO  AS DATA_PAGTO_PAGO,
            l.CLS_CUSTODIARIA     AS CUSTO_DIARIA,
            l.CLS_NUMCONTRATO     AS NUM_CONTRATO,
            l.CLS_FLAG_FECHADO    AS FECHADO
        FROM CAD_LOTESAIDA l
        ORDER BY l.CLS_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_LOTE_SAIDA": req_str(row.get("COD_LOTE_SAIDA")),
            # frigorífico (CAD_ESTGTA); 0 seria o sentinela "sem destino" do TGC
            "COD_DESTINO": opt_code(row.get("COD_DESTINO")),
            "DATA_ABATE": iso_date(row.get("DATA_ABATE")),
            "DATA_EMBARQUE": iso_date(row.get("DATA_EMBARQUE")),
            "QTD_CABECAS": integer(row.get("QTD_CABECAS")),
            # pesos e quantidades: 0 é dado real e viaja como 0.0 / 0
            "CONF_PESO_KG": num(row.get("CONF_PESO_KG")),
            "CONF_QTD_CAB": integer(row.get("CONF_QTD_CAB")),
            "PORT_PESO_KG": num(row.get("PORT_PESO_KG")),
            # exceção do contrato: peso de frigorífico 0 é "não informado" (48/48)
            "FRIG_PESO_KG": _valor(row.get("FRIG_PESO_KG")),
            "FRIG_QTD_CAB": integer(row.get("FRIG_QTD_CAB")),
            "KG_TOTAL_CARCACA": num(row.get("KG_TOTAL_CARCACA")),
            "TOTAL_ARROBA": num(row.get("TOTAL_ARROBA")),
            "RC_PCT": num(row.get("RC_PCT")),
            # valores em R$: 0,00 = "não informado" -> None (ver _valor)
            "VALOR_BRUTO": _valor(row.get("VALOR_BRUTO")),
            "VALOR_LIQUIDO": _valor(row.get("VALOR_LIQUIDO")),
            "VALOR_IMPOSTOS": _valor(row.get("VALOR_IMPOSTOS")),
            "NUMERO_NF": opt_str(row.get("NUMERO_NF")),
            "VALOR_NF": _valor(row.get("VALOR_NF")),
            "DATA_PAGTO_PREV": iso_date(row.get("DATA_PAGTO_PREV")),
            "DATA_PAGTO_PAGO": iso_date(row.get("DATA_PAGTO_PAGO")),
            "CUSTO_DIARIA": _valor(row.get("CUSTO_DIARIA")),
            "NUM_CONTRATO": opt_str(row.get("NUM_CONTRATO")),
            "FECHADO": flag_sn(row.get("FECHADO")),
        }
