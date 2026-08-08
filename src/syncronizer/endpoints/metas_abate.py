"""TGC `metas_abate` endpoint — metas de abate por destino (AUX_DESTINOANIMAL).

Source: Firebird 2.5 `AUX_DESTINOANIMAL` (3 linhas na base real: LEVE 480,
        MÉDIO 520, PESADO 550).
Target: POST /api/integracoes/tgc/metas-abate  body {"metas_abate": [ ... ]}.
Auth:   X-API-Key (ou Authorization: Bearer) = token TGC por fazenda. farm_id vem do
        token no servidor e NÃO viaja no body.

Roda antes de `lotes` (order=16): CLL_DESTINO aponta a meta pelo NOME — a PK real
da tabela é `ADA_NOME` (VARCHAR), não existe código numérico.

A tabela real é SEXADA — não há "PESO_ALVO_KG" único: cada destino carrega peso
alvo, RC e valor de arroba POR SEXO (colunas *M macho / *F fêmea, conferidas no
schema real). O payload espelha os dois sexos; achatar para uma coluna única
inventaria um dado que a fonte não tem.

Escalas (driver já aplica): ADA_PESOABATEM/F BIGINT scale -4 (520.0000 kg),
ADA_PESOABATERCM/F scale -2 (53.00 %), ADA_VALORARROBA_M/F scale -2 (89.00 R$).
"""
from __future__ import annotations

from syncronizer.core.extract import ExtractContext, ExtractSpec
from syncronizer.endpoints._common import BatchEndpoint, num, req_str


class MetasAbateEndpoint(BatchEndpoint):
    name = "metas_abate"             # -> control table ep_metas_abate
    primary_key = "DESTINO"          # PK real da tabela (ADA_NOME, VARCHAR)
    order = 16
    api_path = "/api/integracoes/tgc/metas-abate"  # URL com hifen (padrao de rota do AgroDB); payload_key segue com underscore
    api_method = "POST"

    # Full scan + row_hash — 3 linhas; edição de meta re-envia via hash.
    incremental_column = None
    reconcile_deletes = False

    payload_key = "metas_abate"
    record_key = "DESTINO"
    error_key = "destino"

    _BASE_SQL = """
        SELECT
            d.ADA_NOME          AS DESTINO,
            d.ADA_PESOABATEM    AS PESO_ALVO_KG_M,
            d.ADA_PESOABATEF    AS PESO_ALVO_KG_F,
            d.ADA_PESOABATERCM  AS RC_PCT_M,
            d.ADA_PESOABATERCF  AS RC_PCT_F,
            d.ADA_VALORARROBA_M AS VALOR_ARROBA_M_RS,
            d.ADA_VALORARROBA_F AS VALOR_ARROBA_F_RS
        FROM AUX_DESTINOANIMAL d
        ORDER BY d.ADA_NOME
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        return {
            # NOME é a identidade (é o que CLL_DESTINO do lote referencia)
            "DESTINO": req_str(row.get("DESTINO")),
            "PESO_ALVO_KG_M": num(row.get("PESO_ALVO_KG_M")),
            "PESO_ALVO_KG_F": num(row.get("PESO_ALVO_KG_F")),
            "RC_PCT_M": num(row.get("RC_PCT_M")),
            "RC_PCT_F": num(row.get("RC_PCT_F")),
            "VALOR_ARROBA_M_RS": num(row.get("VALOR_ARROBA_M_RS")),
            "VALOR_ARROBA_F_RS": num(row.get("VALOR_ARROBA_F_RS")),
        }
