from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.curvas_lote import CurvasLoteEndpoint


def test_transform_ordena_os_dias_e_devolve_float():
    """LIST() do Firebird não garante ordem: o transform ordena por dia. A curva do
    lote 10109 começa em 0,78 (semana 1) e sobe para 2,18 (semana 2)."""
    ep = CurvasLoteEndpoint()
    r = ep.transform({"COD_LOTE": 10109, "GMD_LISTA": "8:2.18;1:0.78;2:0.78"})
    assert r["COD_LOTE"] == "10109"
    assert r["GMD_DIARIA"] == [
        {"DIA": 1, "GMD_KG_DIA": 0.78},
        {"DIA": 2, "GMD_KG_DIA": 0.78},
        {"DIA": 8, "GMD_KG_DIA": 2.18},
    ]
    assert ep.make_pk(r) == "10109"


def test_lista_vazia_ou_malformada_nao_derruba_o_lote():
    ep = CurvasLoteEndpoint()
    assert ep.transform({"COD_LOTE": 1, "GMD_LISTA": None})["GMD_DIARIA"] == []
    r = ep.transform({"COD_LOTE": 1, "GMD_LISTA": "1:0.78;lixo;x:y;2:0.80"})
    assert [p["DIA"] for p in r["GMD_DIARIA"]] == [1, 2]


def test_extract_spec_le_a_curva_do_lote_nao_o_catalogo():
    """DET_GMDPROJ_LOTE é a cópia da curva gravada no lote — a que o TGC usa para o
    peso projetado. O catálogo (DET_GMDPROJETADO) pode ser editado depois e é outro
    feed (`curvas`)."""
    ep = CurvasLoteEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "DET_GMDPROJ_LOTE" in spec.sql
    assert "DET_GMDPROJETADO" not in spec.sql
    assert "GROUP BY g.DGP_CODLOTE" in spec.sql and spec.params == ()
    assert spec.incremental is False and ep.incremental_column is None
    assert "LIMIT" not in spec.sql.upper()


def test_roda_logo_depois_de_lotes():
    ep = CurvasLoteEndpoint()
    assert ep.order == 21
    assert ep.payload_key == "curvas_lote"
    assert ep.api_path == "/api/integracoes/tgc/curvas-lote"
