from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.consumo_animais import ConsumoAnimaisEndpoint


def _row(**over):
    base = {
        "COD_ANIMAL": 2005, "COD_LOTE": 10009,
        "DATA_INICIAL": date(2026, 2, 20), "DATA_FINAL": date(2026, 5, 19),
        "DIAS": 89,
        "CONSUMO_MS_KG": 1047.123456789012, "CONSUMO_MN_KG": 1650.5,
        "CUSTO_ALIM": Decimal("1180.337700"),
        "DIAS_DESDE_ENTRADA": 89,
        "CONSUMO_MS_DESDE_ENTRADA_KG": 1047.123456789012,
        "CONSUMO_MN_DESDE_ENTRADA_KG": 1650.5,
        "CUSTO_ALIM_DESDE_ENTRADA": Decimal("1180.337700"),
        "LINHAS": 98, "LINHAS_DESCARTADAS": 0, "LINHAS_POS_SAIDA": 0,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = ConsumoAnimaisEndpoint()
    r = ep.transform(_row())
    assert r["COD_ANIMAL"] == "2005" and r["COD_LOTE"] == "10009"
    assert r["DATA_INICIAL"] == "2026-02-20" and r["DATA_FINAL"] == "2026-05-19"
    assert r["DIAS"] == 89 and isinstance(r["DIAS"], int)
    assert r["CONSUMO_MN_KG"] == 1650.5
    assert r["CUSTO_ALIM"] == 1180.3377
    assert r["LINHAS"] == 98 and r["LINHAS_DESCARTADAS"] == 0 and r["LINHAS_POS_SAIDA"] == 0


def test_chave_e_animal_e_lote():
    """Trocar de lote é manejo normal: o mesmo animal tem uma linha por lote por onde
    passou, e o consumo dele é a soma delas — a chave não pode ser só o animal."""
    ep = ConsumoAnimaisEndpoint()
    a = ep.transform(_row())
    b = ep.transform(_row(COD_LOTE=10020))
    assert a["CHAVE"] == "2005|10009" and ep.make_pk(a) == "2005|10009"
    assert ep.make_pk(a) != ep.make_pk(b)


def test_somas_arredondam_para_o_hash_nao_oscilar():
    """As somas são DOUBLE no Firebird; o ruído das últimas casas mudaria o row_hash
    a cada ciclo e reenviaria 12 mil linhas sem nada ter mudado."""
    ep = ConsumoAnimaisEndpoint()
    a = ep.transform(_row(CONSUMO_MS_KG=1047.123456789012))
    b = ep.transform(_row(CONSUMO_MS_KG=1047.123456789999))
    assert a["CONSUMO_MS_KG"] == 1047.1235 == b["CONSUMO_MS_KG"]


def test_sem_linha_valida_e_zero_dia_e_soma_nula():
    """Animal x lote só com linhas descartadas: 0 dia é dado, soma é ausência — o
    destino não pode receber 0 kg como se o animal tivesse comido nada."""
    ep = ConsumoAnimaisEndpoint()
    r = ep.transform(_row(DIAS=0, CONSUMO_MS_KG=None, CONSUMO_MN_KG=None, CUSTO_ALIM=None,
                          DIAS_DESDE_ENTRADA=0, CONSUMO_MS_DESDE_ENTRADA_KG=None,
                          CONSUMO_MN_DESDE_ENTRADA_KG=None, CUSTO_ALIM_DESDE_ENTRADA=None,
                          DATA_INICIAL=None, DATA_FINAL=None, LINHAS=4, LINHAS_DESCARTADAS=4))
    assert r["DIAS"] == 0 and r["CONSUMO_MS_KG"] is None and r["CUSTO_ALIM"] is None
    assert r["DATA_INICIAL"] is None and r["LINHAS_DESCARTADAS"] == 4


def test_extract_spec_le_as_duas_tabelas_com_os_filtros_do_tgc():
    ep = ConsumoAnimaisEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    sql = spec.sql
    assert "DET_AC_RESUMO_2011" in sql and "DET_AC_RESUMO_INATIVOS" in sql
    assert "UNION ALL" in sql and spec.params == ()
    # D-1 nas duas tabelas
    assert sql.count("DACRES_DATA < CURRENT_DATE") == 2
    # filtro de linha válida do PROC_RESULTADO_INDIVIDUAL
    assert "r.MS > 0 AND r.MN > 0 AND r.CUSTO > 0" in sql
    # consumo depois da saída (morto que segue recebendo trato) fica fora
    assert "r.DATA > a.CA_DATASAIDA" in sql
    # janela desde a entrada no confinamento
    assert "r.DATA >= a.CA_DATAENT" in sql
    assert "GROUP BY x.COD_ANIMAL, x.COD_LOTE" in sql
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in sql.upper()


def test_roda_depois_dos_cadastros():
    ep = ConsumoAnimaisEndpoint()
    assert ep.order == 64
    assert ep.payload_key == "consumo_animais"
    assert ep.api_path == "/api/integracoes/tgc/consumo-animais"
