"""TGC `insumos_entradas` endpoint — cargas de insumo recebidas (entrada do estoque).

Source: Firebird 2.5 `DET_ITENS_NFINSUMOS` (o insumo, o peso recebido e o valor) x
        `CAD_NFINSUMOS` (cabeçalho do recebimento: NF, datas, placa), com o fornecedor
        resolvido por `CAD_COMPRAINSUMOS` -> `CAD_FORNECEDOR`. Na base de staging todo
        cabeçalho tem exatamente 1 item — o "item" É a carga, um caminhão.
Target: POST /api/integracoes/tgc/insumos-entradas  body {"entradas": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

É a ENTRADA do estoque do Relatório de Insumos do AgroDB: saldo = Σ kg recebidos
(este feed) − Σ kg realizados nas batidas (`batidas_itens`) − Σ kg das baixas
(`insumos_baixas`), desde o início do histórico — a mesma conta da Posição de Estoque
do TGC (`PROC_ESTOQUE_POSICAO`). Roda depois do catálogo `alimentos` (order=20), que dá
nome e custo médio ao `COD_ALIMENTO` — zero cargas órfãs, conferido na staging — e
depois das batidas (55/56), no bloco do estoque (order=57).

FULL SCAN + row_hash — centenas de linhas, e a carga É editada depois de gravada: o
lançamento no estoque do TGC (`DNFI_FLAG_CONTAB` vira 'S' e ganha `DNFI_DATA_CONTAB`)
acontece em lote, dias depois do recebimento (93% das cargas foram lançadas depois
do dia em que chegaram), sempre com o mesmo `DNFI_CODIGO`. Um watermark por código
congelaria a carga no estado "não lançada" para sempre. ``reconcile_deletes`` fica
False como nos demais feeds.

Datas — a data da ENTRADA no estoque do AgroDB é o RECEBIMENTO (`CNFI_DATA_REC`,
decisão de 20/10/2026), não a da NF (`CNFI_DATAEMISSAO`) nem a "data de entrega"
(`CNFI_DATAENTREGA`) que a Posição do TGC usa. As três viajam; o destino escolhe.
`CNFI_DATA_REC` ≠ `CNFI_DATAENTREGA` em poucas cargas (há NF de silagem
recebida um mês depois de emitida). Nenhuma das duas é NULL na staging.

`LANCADA_ESTOQUE` = `DNFI_FLAG_CONTAB = 'S'` — o mesmo predicado da Posição do TGC
(NULL ou 'N' são False). Há carga na staging que não está lançada (recebida e
ainda sem `DNFI_DATA_CONTAB`): ela CONTA no saldo do AgroDB,
que a sinaliza na tela — por isso viaja como qualquer outra, sem filtro na origem.

Regra de zero (contrato, seção 12):
  * `QTDE_KG` (DNFI_QTDEENTREGUE, kg RECEBIDOS — o peso que entra no saldo): 0 É dado
    real e viaja como 0.0; NULL viaja None e o destino recusa a LINHA (nenhuma na
    staging). Sem quality gate local, como em todo o agente.
  * `PESO_NF_KG`, `VALOR_UNIT_RS_T`, `VALOR_TOTAL_RS`: 0 vira None (nenhum zero na
    staging hoje; o default "não informado" do TGC é 0).
  * `UMIDADE_PCT` / `IMPUREZA_PCT`: 0,00 é "não medido" e vira None — em quase
    todas as cargas.
  * `TICKET_FAZENDA` (DNFI_TICKETFAZ, INTEGER): 0 vira None, e viaja como TEXTO
    (`ticket_fazenda` é TEXT no espelho). Preenchido (> 0) em poucas cargas; NULL nas
    demais.
  * `PLACA` vazia vira None — e está vazia em TODAS (NULL ou ''). A
    coluna entra pronta para quando a fazenda passar a preencher; sem ela o dado novo
    seria descartado em silêncio.

ARMADILHAS medidas na staging:
  * `VALOR_UNIT_RS_T` é R$ por TONELADA RECEBIDA, não por kg: ex.: 1.004,00 para 50.000 kg
    recebidos e R$ 50.200,00 (= 50.200 / 50 t). Confere em todas as cargas como
    valor_total ÷ kg recebido × 1000 (pelo peso da NF, só 42%). O nome do campo
    carrega a unidade para ninguém multiplicar por kg.
  * kg recebido ≠ kg da NF (`PESO_NF_KG`) em 59% das cargas — é a quebra de
    transporte, informação legítima; o saldo usa SEMPRE o recebido.
  * `NUMERO_NF` é texto livre: há NF que não é um número (faixa "1001-1003" para uma
    carga de várias NFs, palavras no lugar do número, números com vírgula). As palavras são
    lançamentos de ajuste de estoque do operador e entram no saldo do TGC — viajam como
    estão. `SERIE_NF` está vazia em todas ('' ou NULL) e vira None.
  * `CNFI_HORA_REC` é TIME -> texto `HH:MM` (como `batidas`): segundos são cortados
    (01:04:06 -> "01:04") e 00:00 (raro) viaja como está — sem regra de ausência
    para hora no contrato. 10:00 aparece em 53% das cargas: tem cara de hora
    padrão da tela de recebimento, não de hora real — quem ler a hora deve saber disso.
  * Join: o cabeçalho entra por JOIN (item sem cabeçalho não é carga; 0 casos), a
    compra e o fornecedor por LEFT JOIN — uma compra apagada não pode sumir com o
    peso recebido (0 casos hoje; `FORNECEDOR` viajaria None).
  * `DNFI_FLAG_ESTOQUE` é 'S' em todas e não entra: o predicado de "lançada" do TGC
    é o `DNFI_FLAG_CONTAB`.
  * Texto: CHAR/VARCHAR em WIN1252 na origem; com `charset = UTF8` (o padrão do
    agente) o servidor translitera e o driver devolve str (nome de
    fornecedor com Ã e É chega certo). Conferido também com WIN1252.

Escalas: DNFI_QTDEENTREGUE/DNFI_PESOTICKETFORN/DNFI_VALORUN/DNFI_VALORTOTAL/DNFI_UMIDADE/
DNFI_IMPUREZA são BIGINT scale -2 e o driver firebirdsql devolve Decimal com a escala já
aplicada (ex.: 50000.00 kg, 1004.00 R$/t, 11.00 %).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import (
    BatchEndpoint,
    flag_sn,
    hhmm,
    iso_date,
    num,
    opt_code,
    opt_str,
    req_str,
)


def _sem_zero(value):
    """Número, ou None para o 0 de "não informado/não medido" do TGC.

    Vale para peso da NF, valores em R$ e umidade/impureza — nunca para `QTDE_KG`,
    em que 0 é dado real.
    """
    valor = num(value)
    if valor is None or valor == 0:
        return None
    return valor


class InsumosEntradasEndpoint(BatchEndpoint):
    name = "insumos_entradas"        # -> control table ep_insumos_entradas
    primary_key = "COD_ENTRADA"      # PK real da tabela (DNFI_CODIGO)
    order = 57                       # depois de alimentos (20) e das batidas (55/56)
    api_path = "/api/integracoes/tgc/insumos-entradas"  # URL com hífen; payload_key "entradas"
    api_method = "POST"

    # Full scan + row_hash: a carga é lançada no estoque do TGC dias depois de
    # recebida, com o mesmo DNFI_CODIGO — ver docstring.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "entradas"
    record_key = "COD_ENTRADA"
    # A API do AgroDB ecoa o identificador do item em `errors[].chave` (TgcIngestError:
    # {index, chave, message}) — é por ele que a rejeição casa com a linha.
    error_key = "chave"

    _BASE_SQL = """
        SELECT
            d.DNFI_CODIGO         AS COD_ENTRADA,
            d.DNFI_CODNF          AS COD_NF,
            n.CNFI_CODCOMPRA      AS COD_COMPRA,
            d.DNFI_CODALIMENTO    AS COD_ALIMENTO,
            n.CNFI_NUMERO         AS NUMERO_NF,
            n.CNFI_SERIE          AS SERIE_NF,
            n.CNFI_DATAEMISSAO    AS DATA_EMISSAO,
            n.CNFI_DATAENTREGA    AS DATA_ENTREGA,
            n.CNFI_DATA_REC       AS DATA_RECEBIMENTO,
            n.CNFI_HORA_REC       AS HORA_RECEBIMENTO,
            d.DNFI_DATA_CONTAB    AS DATA_LANCAMENTO,
            d.DNFI_FLAG_CONTAB    AS LANCADA_ESTOQUE,
            f.CF_NOME             AS FORNECEDOR,
            n.CNFI_PLACA          AS PLACA,
            d.DNFI_TICKETFAZ      AS TICKET_FAZENDA,
            d.DNFI_QTDEENTREGUE   AS QTDE_KG,
            d.DNFI_PESOTICKETFORN AS PESO_NF_KG,
            d.DNFI_VALORUN        AS VALOR_UNIT_RS_T,
            d.DNFI_VALORTOTAL     AS VALOR_TOTAL_RS,
            d.DNFI_UMIDADE        AS UMIDADE_PCT,
            d.DNFI_IMPUREZA       AS IMPUREZA_PCT
        FROM DET_ITENS_NFINSUMOS d
        JOIN CAD_NFINSUMOS n ON n.CNFI_CODIGO = d.DNFI_CODNF
        LEFT JOIN CAD_COMPRAINSUMOS c ON c.CCIN_CODIGO = n.CNFI_CODCOMPRA
        LEFT JOIN CAD_FORNECEDOR f ON f.CF_CODIGO = c.CCIN_CODFORNECEDOR
        ORDER BY d.DNFI_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            "COD_ENTRADA": req_str(row.get("COD_ENTRADA")),
            "COD_NF": opt_code(row.get("COD_NF")),
            "COD_COMPRA": opt_code(row.get("COD_COMPRA")),
            # join com alimentos_tgc.cod_alimento (AA_CODIGO)
            "COD_ALIMENTO": opt_code(row.get("COD_ALIMENTO")),
            # texto livre: "1001-1003", "AJUSTE"... — nunca converter para número
            "NUMERO_NF": opt_str(row.get("NUMERO_NF")),
            "SERIE_NF": opt_str(row.get("SERIE_NF")),
            "DATA_EMISSAO": iso_date(row.get("DATA_EMISSAO")),
            "DATA_ENTREGA": iso_date(row.get("DATA_ENTREGA")),
            # a data da entrada no estoque do AgroDB (ver docstring)
            "DATA_RECEBIMENTO": iso_date(row.get("DATA_RECEBIMENTO")),
            # TIME -> "HH:MM"; 00:00 viaja como está (ver docstring)
            "HORA_RECEBIMENTO": hhmm(row.get("HORA_RECEBIMENTO")),
            "DATA_LANCAMENTO": iso_date(row.get("DATA_LANCAMENTO")),
            # DNFI_FLAG_CONTAB = 'S'; False = recebida e ainda não lançada no TGC
            "LANCADA_ESTOQUE": flag_sn(row.get("LANCADA_ESTOQUE")),
            "FORNECEDOR": opt_str(row.get("FORNECEDOR")),
            "PLACA": opt_str(row.get("PLACA")),
            # INTEGER no TGC, TEXT no espelho; 0 = sem ticket -> None
            "TICKET_FAZENDA": opt_code(row.get("TICKET_FAZENDA")),
            # kg recebidos: 0 é dado real (viaja 0.0); NULL -> None e o destino recusa a linha
            "QTDE_KG": num(row.get("QTDE_KG")),
            "PESO_NF_KG": _sem_zero(row.get("PESO_NF_KG")),
            # R$ por TONELADA, não por kg (ver docstring)
            "VALOR_UNIT_RS_T": _sem_zero(row.get("VALOR_UNIT_RS_T")),
            "VALOR_TOTAL_RS": _sem_zero(row.get("VALOR_TOTAL_RS")),
            # 0,00 = não medido -> None
            "UMIDADE_PCT": _sem_zero(row.get("UMIDADE_PCT")),
            "IMPUREZA_PCT": _sem_zero(row.get("IMPUREZA_PCT")),
        }
