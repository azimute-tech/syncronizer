import json
from datetime import date
from decimal import Decimal

from syncronizer.core.extract import ExtractContext
from syncronizer.endpoints.animais import AnimaisEndpoint


def _row(**over):
    base = {
        "COD_ANIMAL": 1, "SISBOV": None, "CHIP": None, "NCF": None,
        "CATEGORIA": "BOI INTEIRO", "RACA": "NELORE ",
        "LOTE_ENTRADA": 10001, "LOTE_ATUAL": 10020, "CURRAL_ATUAL": 230,
        "DATA_REGISTRO": date(2026, 1, 28), "DATA_ENTRADA": date(2026, 1, 28),
        "PESO_ENTRADA": Decimal("315.00"), "VALOR_ENTRADA": Decimal("0.00"),
        "RC_ENTRADA": Decimal("50.00"), "IDADE": 15,
        "NUM_CONTRATO": "1",
        "SAIDA": "NENHUM", "CAUSA_MORTE": None, "DATA_SAIDA": None, "PESO_SAIDA": None,
        "VALOR_SAIDA": Decimal("0.00"),
        "COD_LOTE_SAIDA": None, "RC_SAIDA": None, "ARROBAS_SAIDA": None,
    }
    base.update(over)
    return base


def test_transform_shape_and_types():
    ep = AnimaisEndpoint()
    r = ep.transform(_row())
    assert r["COD_ANIMAL"] == "1"
    assert r["LOTE_ENTRADA"] == "10001" and r["LOTE_ATUAL"] == "10020"
    assert r["PESO_ENTRADA"] == 315.0 and isinstance(r["PESO_ENTRADA"], float)
    assert r["VALOR_ENTRADA"] is None    # 0,00 do TGC = não informado, nunca custo real
    assert r["RC_ENTRADA"] == 50.0 and r["IDADE"] == 15
    assert r["DATA_ENTRADA"] == "2026-01-28"
    assert r["CATEGORIA"] == "BOI INTEIRO"
    assert r["CURRAL_ATUAL"] == "230"    # CC_CODIGO, not CC_NOME — see below
    assert r["RACA"] == "NELORE"         # VARCHAR padding stripped
    # NENHUM/None optional fields omitted (mirrors validated payload)
    assert "SAIDA" not in r and "SISBOV" not in r and "CHIP" not in r and "NCF" not in r
    assert "CAUSA_MORTE" not in r
    assert ep.make_pk(r) == "1"


def test_transform_sends_data_registro():
    """CA_DATAREG is the immutable creation date — it must travel alongside DATA_ENTRADA,
    which the user can edit."""
    ep = AnimaisEndpoint()
    r = ep.transform(_row(DATA_REGISTRO=date(2026, 1, 28), DATA_ENTRADA=date(2026, 3, 10)))
    assert r["DATA_REGISTRO"] == "2026-01-28"
    assert r["DATA_ENTRADA"] == "2026-03-10"


def test_curral_atual_is_the_code_never_the_name():
    """AgroDB joins animais_tgc.cod_curral_atual against currais_tgc.cod_curral
    (= CC_CODIGO) in vw_ocupacao_currais. Sending CC_NOME makes that join miss
    SILENTLY — the animal just disappears from the occupancy screen. The code is also
    the stable key: renaming a curral on the farm would break the history."""
    ep = AnimaisEndpoint()
    spec = ep.extract_spec(ExtractContext(last_watermark=None))
    assert "a.CA_ULTIMO_CURRAL   AS CURRAL_ATUAL" in spec.sql
    assert "CC_NOME" not in spec.sql          # the name is NOT denormalized here
    assert "CAD_CURRAL" not in spec.sql       # join dropped with it

    r = ep.transform(_row(CURRAL_ATUAL=230))
    assert r["CURRAL_ATUAL"] == "230"
    # 0 / NULL are TGC's "no curral" sentinels, not a curral called "0"
    assert ep.transform(_row(CURRAL_ATUAL=0))["CURRAL_ATUAL"] is None
    assert ep.transform(_row(CURRAL_ATUAL=None))["CURRAL_ATUAL"] is None


def test_transform_keeps_real_exit_and_lote_fallback():
    ep = AnimaisEndpoint()
    r = ep.transform(_row(SAIDA="MORTE", LOTE_ATUAL=None, DATA_SAIDA=date(2026, 4, 8)))
    assert r["SAIDA"] == "MORTE"
    assert r["DATA_SAIDA"] == "2026-04-08"
    assert r["LOTE_ATUAL"] == "10001"  # falls back to LOTE_ENTRADA when null


def test_exit_block_only_for_animals_that_left():
    """TGC leaves CA_VALORSAIDA at 0,00 for the whole live herd; a live animal must not
    be shipped with a fake 'exit worth zero'."""
    ep = AnimaisEndpoint()
    live = ep.transform(_row(VALOR_SAIDA=Decimal("0.00")))
    assert "VALOR_SAIDA" not in live and "DATA_SAIDA" not in live

    sold = ep.transform(_row(SAIDA="VENDA", DATA_SAIDA=date(2026, 6, 1),
                             PESO_SAIDA=Decimal("520.00"), VALOR_SAIDA=Decimal("7300.50")))
    assert sold["PESO_SAIDA"] == 520.0 and sold["VALOR_SAIDA"] == 7300.5


def test_valor_entrada_zero_do_tgc_vira_none():
    """CA_VALORENT fica 0,00 quando a fazenda não informa o custo (backup real de
    Birigui: 0% das linhas preenchidas). 0 é "não informado", não um animal que custou
    R$ 0 — encaminhar 0,00 plantaria um custo falso justamente no campo que o AgroDB
    usa como fallback. Mesmo racional do VALOR_SAIDA 0,00 do rebanho vivo; contraste
    deliberado com PESO_ENTRADA=0, que É enviado (problema de qualidade)."""
    ep = AnimaisEndpoint()
    assert ep.transform(_row(VALOR_ENTRADA=Decimal("0.00")))["VALOR_ENTRADA"] is None
    assert ep.transform(_row(VALOR_ENTRADA=None))["VALOR_ENTRADA"] is None
    # quando a fazenda preencher, o valor viaja como float — o driver firebirdsql já
    # devolve Decimal com a escala aplicada (INTEGER scale -2, centavos)
    r = ep.transform(_row(VALOR_ENTRADA=Decimal("8532.50")))
    assert r["VALOR_ENTRADA"] == 8532.5 and isinstance(r["VALOR_ENTRADA"], float)
    # peso zerado segue o caminho oposto: viaja para o destino apontar o problema
    assert ep.transform(_row(PESO_ENTRADA=Decimal("0.00")))["PESO_ENTRADA"] == 0.0


def test_causa_morte_viaja_com_a_morte():
    """Morte é a única saída tratada no escopo atual e precisa vir COM a causa
    (CA_CAUSAMORTE — 39 mortes com 16 causas distintas na base real). Padding de
    CHAR/VARCHAR é removido como nos demais campos."""
    ep = AnimaisEndpoint()
    r = ep.transform(_row(SAIDA="MORTE", DATA_SAIDA=date(2026, 4, 8),
                          CAUSA_MORTE="PNEUMONIA "))
    assert r["SAIDA"] == "MORTE"
    assert r["CAUSA_MORTE"] == "PNEUMONIA"
    # morte sem causa registrada: o campo é omitido, nunca "" (padrão do módulo)
    sem_causa = ep.transform(_row(SAIDA="MORTE", CAUSA_MORTE=None))
    assert sem_causa["SAIDA"] == "MORTE" and "CAUSA_MORTE" not in sem_causa
    assert "CAUSA_MORTE" not in ep.transform(_row(SAIDA="MORTE", CAUSA_MORTE="  "))


def test_causa_morte_preenchida_viaja_mesmo_fora_do_bloco_de_saida():
    """O envio NÃO é condicionado a SAIDA=MORTE: dado preenchido viaja (na base real
    só existe em MORTE, mas um preenchimento fora do padrão no TGC não pode ser
    descartado em silêncio pelo espelho)."""
    ep = AnimaisEndpoint()
    vivo = ep.transform(_row(CAUSA_MORTE="PNEUMONIA"))       # SAIDA=NENHUM
    assert "SAIDA" not in vivo and vivo["CAUSA_MORTE"] == "PNEUMONIA"


def test_transform_drops_corrupted_chip():
    """'9,63E+14' is an Excel-destroyed chip (66 rows in the client base) — dropping it
    is not a quality gate, it is refusing to hand 66 animals the same fake identifier."""
    ep = AnimaisEndpoint()
    r = ep.transform(_row(CHIP="9,63E+14", SISBOV="105000012345678"))
    assert "CHIP" not in r
    assert r["SISBOV"] == "105000012345678"  # a good identifier still travels


def test_transform_keeps_valid_chip_and_ncf():
    ep = AnimaisEndpoint()
    r = ep.transform(_row(CHIP="963000407320194", NCF="12345"))
    assert r["CHIP"] == "963000407320194" and r["NCF"] == "12345"


def test_extract_spec_builds_query():
    spec = AnimaisEndpoint().extract_spec(ExtractContext(last_watermark=None))
    assert "CAD_ANIMAL" in spec.sql and spec.params == ()
    assert "CAD_RACA" in spec.sql and "CA_DATAREG" in spec.sql
    assert "a.CA_VALORENT        AS VALOR_ENTRADA" in spec.sql
    assert "a.CA_CAUSAMORTE      AS CAUSA_MORTE" in spec.sql
    assert "a.CA_LOTESAIDA       AS COD_LOTE_SAIDA" in spec.sql
    assert "a.CA_RC_SAIDA        AS RC_SAIDA" in spec.sql
    assert "a.CA_PESO_ARR_SAIDA  AS ARROBAS_SAIDA" in spec.sql
    # CA_RC_SAIDA_PROJ é o RC PROJETADO (outra coluna da mesma tabela): o fechamento
    # quer o RC realizado, então a projeção não entra no espelho
    assert "CA_RC_SAIDA_PROJ" not in spec.sql
    assert spec.incremental is False


def test_bloco_de_abate_viaja_fora_do_gate_do_bloco_de_saida():
    """CA_LOTESAIDA é a CHAVE do fechamento (3.184 dos 9.392 animais da base real,
    exatamente a contagem das pesagens de tipo SAIDA) e CA_RC_SAIDA/CA_PESO_ARR_SAIDA
    são o resultado individual do abate. Os três ficam FORA do bloco condicional de
    saída de propósito: ali o gate existe porque CA_VALORSAIDA fica 0,00 no rebanho
    vivo inteiro, e aqui não há sentinela zero a filtrar — na base real essas colunas
    são NULL ou têm valor, nunca 0."""
    ep = AnimaisEndpoint()
    r = ep.transform(_row(COD_LOTE_SAIDA=24, RC_SAIDA=Decimal("49.74"),
                          ARROBAS_SAIDA=Decimal("9.80")))
    assert r["COD_LOTE_SAIDA"] == "24"      # join com lotes_saida_tgc.cod_lote_saida
    assert r["RC_SAIDA"] == 49.74 and r["ARROBAS_SAIDA"] == 9.8
    # animal vivo: as chaves existem no payload com None, nunca somem
    vivo = ep.transform(_row())
    assert vivo["COD_LOTE_SAIDA"] is None
    assert vivo["RC_SAIDA"] is None and vivo["ARROBAS_SAIDA"] is None


def test_animal_em_lote_de_abate_sem_rc_individual():
    """Assimetria real da base: 3.184 animais têm CA_LOTESAIDA mas só 2.258 têm
    CA_RC_SAIDA. Os 926 restantes têm que chegar ao destino como ausência (None), não
    como zero."""
    r = AnimaisEndpoint().transform(_row(COD_LOTE_SAIDA=8, RC_SAIDA=None,
                                         ARROBAS_SAIDA=None))
    assert r["COD_LOTE_SAIDA"] == "8"
    assert r["RC_SAIDA"] is None and r["ARROBAS_SAIDA"] is None


class _Resp:
    def __init__(self, data):
        self._data = data
        self.content = b"{}"

    def json(self):
        return self._data


class _Http:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return _Resp(self.data)


def _unsent(cod):
    a = AnimaisEndpoint().transform(_row(COD_ANIMAL=cod, DATA_ENTRADA=date(2020, 1, 1)))
    return {"pk": str(cod), "payload": json.dumps(a), "deleted": False}


def test_send_batches_and_reconciles_per_item_errors():
    ep = AnimaisEndpoint()
    rows = [_unsent(1), _unsent(2), _unsent(3)]
    http = _Http({"inserted": 2, "updated": 0, "errors": [{"cod_animal": "3", "message": "dup"}]})
    res = ep.send(http, rows)
    assert set(res.ok) == {"1", "2"}
    assert [pk for pk, _ in res.failed] == ["3"]
    # one POST with the batch wrapped under "animais"
    assert len(http.calls) == 1
    method, path, body = http.calls[0]
    assert method == "POST" and path == "/api/integracoes/tgc/animais"
    assert isinstance(body["animais"], list) and len(body["animais"]) == 3


def test_send_matches_errors_when_api_echoes_numeric_id():
    """The API may echo cod_animal as a JSON number; the row must still be marked failed."""
    ep = AnimaisEndpoint()
    http = _Http({"inserted": 1, "errors": [{"cod_animal": 3, "message": "dup"}]})
    res = ep.send(http, [_unsent(1), _unsent(3)])
    assert res.ok == ["1"] and [pk for pk, _ in res.failed] == ["3"]


def test_send_sends_everything_including_quality_issues():
    """No local quality gate: an animal the API may dislike (peso 0, RC 150) is still
    POSTed — the destination surfaces it and the fix happens in TGC."""
    ep = AnimaisEndpoint()
    quality_issue = {"pk": "9",
                     "payload": json.dumps({"COD_ANIMAL": "9", "PESO_ENTRADA": 0, "RC_ENTRADA": 150}),
                     "deleted": False}
    http = _Http({"inserted": 2, "updated": 0, "errors": []})
    res = ep.send(http, [_unsent(1), quality_issue])
    assert set(res.ok) == {"1", "9"}                  # nothing dropped locally
    assert len(http.calls[0][2]["animais"]) == 2      # the "bad" animal was POSTed too


def test_send_does_not_fail_chunk_on_unexpected_success_body():
    """A 200 whose body is not the expected object must not re-queue sent rows forever."""

    class _WeirdResp:
        content = b"[]"

        def json(self):
            return []

    class _WeirdHttp:
        def request(self, method, path, json=None):
            return _WeirdResp()

    res = AnimaisEndpoint().send(_WeirdHttp(), [_unsent(1), _unsent(2)])
    assert set(res.ok) == {"1", "2"} and res.failed == []


def test_no_farm_id_in_payload():
    """farm_id comes from the token server-side; it must never be in the body."""
    ep = AnimaisEndpoint()
    http = _Http({"inserted": 1, "errors": []})
    ep.send(http, [_unsent(1)])
    body = http.calls[0][2]
    assert "farm_id" not in body
    assert all("farm_id" not in item for item in body["animais"])
