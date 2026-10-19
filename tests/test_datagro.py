"""Testes do coletor DATAGRO (Indicador do Boi SP) — tudo mockado (sem rede real).

Fixtures capturadas em 17/10/2026 (PNG embutido removido, ``<text>`` intactos):
- ``datagro_boletim_cinco.svg``: 5 dias úteis por estado; SP = 09/Out 378,50 ·
  13/Out 378,33 · 14/Out 379,20 · 15/Out 380,00 · 16/Out 380,42 · média 379,29.
- ``datagro_boi.svg``: valor do dia por estado + a data ``16/Out/2026``.
- ``datagro_quadro.json``: ``quadros`` do ativo D_PEPR_SP_BR (dia 2026-10-16, ult 380.42).
"""
import json
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncronizer.indicadores import datagro, job
from syncronizer.indicadores.comum import IndicadoresError

log = logging.getLogger("test.datagro")

FIX = Path(__file__).parent / "fixtures"
BOLETIM = (FIX / "datagro_boletim_cinco.svg").read_text(encoding="utf-8")
BOI = (FIX / "datagro_boi.svg").read_text(encoding="utf-8")
QUADRO = json.loads((FIX / "datagro_quadro.json").read_text(encoding="utf-8"))

HOJE = date(2026, 10, 17)
SP_ESPERADO = [
    {"data": "2026-10-09", "valor_rs": 378.50},
    {"data": "2026-10-13", "valor_rs": 378.33},
    {"data": "2026-10-14", "valor_rs": 379.20},
    {"data": "2026-10-15", "valor_rs": 380.00},
    {"data": "2026-10-16", "valor_rs": 380.42},
]


# --------------------------------------------------------------------------- #
# helpers: SVG sintético com a MESMA estrutura de <text> do boletim real
# --------------------------------------------------------------------------- #
def _text(s):
    return f'<text x="10" y="1" font-weight="bold">{s}</text>'


def _boletim(datas, sp, media, outros=(("BA", ["349,17"] * 5, "349,17"),)):
    """Boletim sintético: datas, 'Média|5 dias' em tspans, linha de SP e outras UFs."""
    partes = [_text(d) for d in datas]
    partes.append('<text x="10" y="490"> <tspan x="10" dy="-9">Média</tspan>\n\t\t  '
                  '<tspan x="10" dy="19">5 dias</tspan></text>')
    for uf, valores, med in ((("SP", sp, media),) + tuple(outros)):
        partes.append(_text(uf))
        partes += [_text(v) for v in valores]
        partes.append(_text(med))
    return '<svg xmlns="http://www.w3.org/2000/svg"><g><image xlink:href="x"/>' + "\n".join(partes) + "</g></svg>"


SP_OUT = ["378,50", "378,33", "379,20", "380,00", "380,42"]


class _Resp:
    def __init__(self, text="", data=None, status=200):
        self.text = text
        self._data = data
        self.status_code = status
        self.content = b"{}"

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Http:
    """Fake do app.http: registra as chamadas (method, path, json)."""

    def __init__(self, post_resp=None, raise_exc=None):
        self.post_resp = post_resp if post_resp is not None else {"data": {"inserted": 1, "updated": 4}}
        self.raise_exc = raise_exc
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(data=self.post_resp)


def _patch_fontes(monkeypatch, boletim=BOLETIM, boi=BOI, quadro=QUADRO,
                  boletim_status=200, boi_exc=None, quadro_exc=None):
    """Patch do requests.get do módulo datagro por URL; devolve as chamadas capturadas."""
    import requests as _requests
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        if url == datagro.BOLETIM_URL:
            return _Resp(text=boletim, status=boletim_status)
        if url == datagro.BOI_URL:
            if boi_exc is not None:
                raise boi_exc
            return _Resp(text=boi)
        if url == datagro.QUADRO_URL:
            if quadro_exc is not None:
                raise quadro_exc
            return _Resp(data=quadro)
        raise AssertionError(f"URL inesperada: {url}")

    monkeypatch.setattr(datagro.requests, "get", fake_get)
    return calls


# --------------------------------------------------------------------------- #
# parse_boletim_cinco: fixture REAL
# --------------------------------------------------------------------------- #
def test_parse_boletim_fixture_real():
    itens = datagro.parse_boletim_cinco(BOLETIM, HOJE)
    assert len(itens) == 5
    assert [i["data"] for i in itens] == [e["data"] for e in SP_ESPERADO]
    assert [i["valor_rs"] for i in itens] == pytest.approx([e["valor_rs"] for e in SP_ESPERADO])


def test_parse_boletim_so_sp_nao_pega_estado_seguinte():
    # BA vem logo depois de SP com valores diferentes: nenhum pode vazar.
    itens = datagro.parse_boletim_cinco(BOLETIM, HOJE)
    assert all(370 < i["valor_rs"] < 390 for i in itens)


def test_parse_boletim_sintetico_mesma_estrutura():
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"], SP_OUT, "379,29")
    _aprox(datagro.parse_boletim_cinco(svg, HOJE), SP_ESPERADO)


def _aprox(itens, esperado):
    assert [i["data"] for i in itens] == [e["data"] for e in esperado]
    assert [i["valor_rs"] for i in itens] == pytest.approx([e["valor_rs"] for e in esperado])
    return True


# --------------------------------------------------------------------------- #
# regra do ano: corrente; mês à frente do mês de hoje -> ano anterior (virada)
# --------------------------------------------------------------------------- #
def test_parse_boletim_virada_de_ano():
    svg = _boletim(["26/Dez", "29/Dez", "30/Dez", "31/Dez", "02/Jan"],
                   ["350,00", "351,00", "352,00", "353,00", "354,00"], "352,00")
    itens = datagro.parse_boletim_cinco(svg, date(2027, 1, 5))
    assert [i["data"] for i in itens] == [
        "2026-12-26", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-02",
    ]


def test_parse_boletim_meses_pt_br_case_insensitive():
    svg = _boletim(["02/mai", "03/MAI", "04/Mai", "05/Mai", "06/Mai"],
                   ["300,00", "300,00", "300,00", "300,00", "300,00"], "300,00")
    itens = datagro.parse_boletim_cinco(svg, date(2026, 5, 7))
    assert itens[0]["data"] == "2026-05-02" and itens[-1]["data"] == "2026-05-06"


def test_inferir_data_mes_igual_ao_de_hoje_e_ano_corrente():
    assert datagro.inferir_data(16, 10, date(2026, 10, 17)) == date(2026, 10, 16)
    assert datagro.inferir_data(30, 12, date(2027, 1, 2)) == date(2026, 12, 30)
    assert datagro.inferir_data(2, 1, date(2027, 1, 2)) == date(2027, 1, 2)


def test_parse_boletim_data_no_futuro_raises():
    # relógio da máquina atrasado (hoje=15/Out mas o boletim já mostra 16/Out)
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"], SP_OUT, "379,29")
    with pytest.raises(IndicadoresError, match="futuro"):
        datagro.parse_boletim_cinco(svg, date(2026, 10, 15))


def test_parse_boletim_datas_nao_crescentes_raises():
    svg = _boletim(["09/Out", "13/Out", "13/Out", "15/Out", "16/Out"], SP_OUT, "379,29")
    with pytest.raises(IndicadoresError, match="fora de ordem"):
        datagro.parse_boletim_cinco(svg, HOJE)


# --------------------------------------------------------------------------- #
# formato quebrado / valor fora da faixa -> IndicadoresError (nada é enviado)
# --------------------------------------------------------------------------- #
def test_parse_boletim_menos_de_5_datas_raises():
    svg = _boletim(["14/Out", "15/Out", "16/Out"], SP_OUT[:3], "379,00")
    with pytest.raises(IndicadoresError, match="3 data"):
        datagro.parse_boletim_cinco(svg, HOJE)


def test_parse_boletim_sem_sp_raises():
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"], SP_OUT, "379,29")
    svg = svg.replace(">SP<", ">SX<")
    with pytest.raises(IndicadoresError, match="sem a linha de SP"):
        datagro.parse_boletim_cinco(svg, HOJE)


def test_parse_boletim_svg_vazio_raises():
    with pytest.raises(IndicadoresError, match="formato mudou"):
        datagro.parse_boletim_cinco("<svg>mudou tudo</svg>", HOJE)


def test_parse_boletim_valor_fora_da_faixa_raises():
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"],
                   ["378,50", "378,33", "379,20", "380,00", "1.380,42"], "579,29")
    with pytest.raises(IndicadoresError, match="faixa"):
        datagro.parse_boletim_cinco(svg, HOJE)


def test_parse_boletim_valor_nao_numerico_na_linha_sp_raises():
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"],
                   ["378,50", "378,33", "n/d", "380,00", "380,42"], "379,29")
    with pytest.raises(IndicadoresError, match="esperava valor"):
        datagro.parse_boletim_cinco(svg, HOJE)


def test_parse_boletim_media_desalinhada_raises():
    # média exibida não bate com os 5 valores: pegamos a linha errada -> não envia
    svg = _boletim(["09/Out", "13/Out", "14/Out", "15/Out", "16/Out"], SP_OUT, "349,17")
    with pytest.raises(IndicadoresError, match="média"):
        datagro.parse_boletim_cinco(svg, HOJE)


# --------------------------------------------------------------------------- #
# parse_boi_data / parse_quadro_sp
# --------------------------------------------------------------------------- #
def test_parse_boi_data_fixture_real():
    assert datagro.parse_boi_data(BOI) == date(2026, 10, 16)


def test_parse_boi_data_ausente_retorna_none():
    assert datagro.parse_boi_data("<svg><text>380,42</text></svg>") is None
    assert datagro.parse_boi_data("") is None


def test_parse_quadro_sp_fixture_real():
    assert datagro.parse_quadro_sp(QUADRO) == {"data": "2026-10-16", "valor_rs": pytest.approx(380.42)}


def test_parse_quadro_sp_formato_inesperado_retorna_none():
    assert datagro.parse_quadro_sp({}) is None
    assert datagro.parse_quadro_sp({"ativos": [{"cod": "OUTRO", "dados": {}}]}) is None
    assert datagro.parse_quadro_sp({"ativos": [{"cod": "D_PEPR_SP_BR", "dados": {"dia": "x", "ult": "1"}}]}) is None
    assert datagro.parse_quadro_sp("lixo") is None


# --------------------------------------------------------------------------- #
# fetch: sessão nova, UA de browser, timeout largo, non-2xx -> erro
# --------------------------------------------------------------------------- #
def test_fetch_boletim_user_agent_de_browser_sem_auth(monkeypatch):
    calls = _patch_fontes(monkeypatch)
    svg = datagro.fetch_boletim(log)
    assert "<text" in svg
    assert calls[0]["url"] == datagro.BOLETIM_URL
    assert "Mozilla/5.0" in calls[0]["headers"]["User-Agent"]
    assert "X-API-Key" not in calls[0]["headers"]
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["timeout"] == (10, 60)


def test_fetch_boletim_non_2xx_raises(monkeypatch):
    _patch_fontes(monkeypatch, boletim_status=503)
    with pytest.raises(IndicadoresError, match="503"):
        datagro.fetch_boletim(log)


# --------------------------------------------------------------------------- #
# run_datagro: payload exato (5 itens, FONTE = URL do boletim), conferências
# --------------------------------------------------------------------------- #
def test_run_datagro_happy_path(monkeypatch):
    _patch_fontes(monkeypatch)
    http = _Http(post_resp={"data": {"inserted": 1, "updated": 4}})

    bloco = datagro.run_datagro(http, log, hoje=HOJE)

    assert bloco["ok"] is True
    assert bloco["indicador"] == "DATAGRO_BOI_GORDO_SP"
    assert bloco["datas"] == [e["data"] for e in SP_ESPERADO]
    assert bloco["data_indicador"] == "2026-10-16"
    assert bloco["valor_rs"] == pytest.approx(380.42)
    assert bloco["quadro"] == "ok"
    assert bloco["inserted"] == 1 and bloco["updated"] == 4
    # UM POST só, com os 5 itens no contrato da rota
    assert [(c[0], c[1]) for c in http.calls] == [("POST", "/api/integracoes/indicadores")]
    itens = http.calls[0][2]["indicadores"]
    assert len(itens) == 5
    assert itens[-1] == {"INDICADOR": "DATAGRO_BOI_GORDO_SP", "DATA": "2026-10-16",
                         "VALOR_RS": pytest.approx(380.42), "FONTE": datagro.BOLETIM_URL}
    assert {tuple(sorted(i.keys())) for i in itens} == {("DATA", "FONTE", "INDICADOR", "VALOR_RS")}


def test_run_datagro_quadro_divergente_so_avisa(monkeypatch, caplog):
    quadro = json.loads(json.dumps(QUADRO))
    quadro["ativos"][0]["dados"]["ult"] = "381.00"
    _patch_fontes(monkeypatch, quadro=quadro)
    http = _Http()
    with caplog.at_level(logging.WARNING, logger="test.datagro"):
        bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is True                 # o boletim é o oficial: vai mesmo assim
    assert bloco["quadro"] == "divergente"
    assert len(http.calls) == 1
    assert http.calls[0][2]["indicadores"][-1]["VALOR_RS"] == pytest.approx(380.42)
    assert any("quadros JSON" in r.getMessage() and "≠" in r.getMessage() for r in caplog.records)


def test_run_datagro_quadro_fora_do_ar_segue_so_com_boletim(monkeypatch):
    import requests as _requests
    _patch_fontes(monkeypatch, quadro_exc=_requests.ConnectionError("401/timeout"))
    http = _Http()
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is True
    assert bloco["quadro"] == "indisponivel"
    assert len(http.calls) == 1


def test_run_datagro_boi_svg_fora_do_ar_nao_bloqueia(monkeypatch):
    import requests as _requests
    _patch_fontes(monkeypatch, boi_exc=_requests.ReadTimeout("lento"))
    http = _Http()
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is True and len(http.calls) == 1


def test_run_datagro_ano_divergente_do_boi_svg_falha_fechado(monkeypatch):
    # boi.svg diz 16/Out/2025 enquanto o boletim foi inferido como 2026 -> não envia
    _patch_fontes(monkeypatch, boi=BOI.replace("16/Out/2026", "16/Out/2025"))
    http = _Http()
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is False
    assert "ano inferido" in bloco["error"]
    assert http.calls == []


def test_run_datagro_boletim_quebrado_nao_envia(monkeypatch):
    _patch_fontes(monkeypatch, boletim="<svg>mudou</svg>")
    http = _Http()
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is False and "formato mudou" in bloco["error"]
    assert http.calls == []


def test_run_datagro_api_200_com_errors_e_falha(monkeypatch):
    # Caso real do 1º disparo: AgroDB sem DATAGRO na whitelist responde 200 com
    # errors por item e inserted=updated=0 — NÃO pode virar ok=True.
    _patch_fontes(monkeypatch)
    http = _Http(post_resp={"inserted": 0, "updated": 0, "avisos": [], "errors": [
        {"index": i, "chave": "DATAGRO_BOI_GORDO_SP",
         "message": 'INDICADOR "DATAGRO_BOI_GORDO_SP" fora do dominio'}
        for i in range(5)]})
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is False
    assert "rejeitou 5 de 5" in bloco["error"] and "fora do dominio" in bloco["error"]


def test_run_datagro_api_rejeita_um_item_e_falha(monkeypatch):
    _patch_fontes(monkeypatch)
    http = _Http(post_resp={"inserted": 0, "updated": 4, "avisos": [], "errors": [
        {"index": 4, "chave": "DATAGRO_BOI_GORDO_SP", "message": "DATA no futuro"}]})
    bloco = datagro.run_datagro(http, log, hoje=HOJE)
    assert bloco["ok"] is False and "rejeitou 1 de 5" in bloco["error"]


def test_run_datagro_api_sem_contar_todos_e_falha(monkeypatch):
    _patch_fontes(monkeypatch)
    bloco = datagro.run_datagro(_Http(post_resp={"inserted": 1, "updated": 1, "errors": []}), log, hoje=HOJE)
    assert bloco["ok"] is False and "confirmou só 2 de 5" in bloco["error"]


def test_run_datagro_post_falha_nao_levanta(monkeypatch):
    _patch_fontes(monkeypatch)
    bloco = datagro.run_datagro(_Http(raise_exc=RuntimeError("500 Server Error")), log, hoje=HOJE)
    assert bloco["ok"] is False and "500" in bloco["error"]


# --------------------------------------------------------------------------- #
# orquestrador (job) com o coletor de verdade (fontes mockadas por URL)
# --------------------------------------------------------------------------- #
def _settings(**over):
    base = dict(api_base_url="https://api.example.com", api_key="k", api_token="",
                indicadores_enabled=True, indicadores_hour=20, indicadores_minute=30,
                tz_offset_hours=-3)
    base.update(over)
    return SimpleNamespace(**base)


def _paths(tmp_path):
    d = tmp_path / "state" / "indicadores"
    d.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(indicadores_dir=d)


def _fixar_dia(monkeypatch, hora=21):
    from datetime import datetime
    monkeypatch.setattr(job.timewin, "local_today", lambda s, now_utc=None: "2026-10-17")
    monkeypatch.setattr(job.timewin, "local_now", lambda s, now_utc=None: datetime(2026, 10, 17, hora, 0))


def test_job_envia_datagro_e_grava_status(tmp_path, monkeypatch):
    _patch_fontes(monkeypatch)
    _fixar_dia(monkeypatch, hora=20)
    http = _Http()
    paths = _paths(tmp_path)
    s = _settings()

    status = job.run_indicadores(s, paths, http, log)

    assert status["ok"] is True
    assert status["datagro"]["datas"][-1] == "2026-10-16"   # hoje do job = dia LOCAL
    assert len(http.calls) == 1
    assert {i["INDICADOR"] for i in http.calls[0][2]["indicadores"]} == {"DATAGRO_BOI_GORDO_SP"}
    saved = json.loads((paths.indicadores_dir / "last_indicadores.json").read_text())
    assert saved["ok"] is True and saved["datagro"]["ok"] is True
    assert job.indicadores_done_today(paths, s) is True


def test_job_api_rejeita_e_catchup_retenta(tmp_path, monkeypatch):
    _patch_fontes(monkeypatch)
    _fixar_dia(monkeypatch)
    paths = _paths(tmp_path)
    s = _settings()
    http = _Http(post_resp={"inserted": 0, "updated": 0, "errors": [
        {"index": 0, "chave": "DATAGRO_BOI_GORDO_SP", "message": "fora do dominio"}]})
    status = job.run_indicadores(s, paths, http, log)
    assert status["ok"] is False and status["error"].startswith("datagro: API rejeitou 1 de 5")
    assert job.indicadores_done_today(paths, s) is False


def test_job_catchup_reaproveita_datagro_ok_do_dia(tmp_path, monkeypatch):
    _patch_fontes(monkeypatch)
    _fixar_dia(monkeypatch)
    paths = _paths(tmp_path)
    s = _settings()
    (paths.indicadores_dir / "last_indicadores.json").write_text(json.dumps({
        "ok": True, "data_referencia": "2026-10-17",
        "datagro": {"ok": True, "indicador": "DATAGRO_BOI_GORDO_SP", "datas": ["2026-10-16"],
                    "valores_rs": [380.42], "data_indicador": "2026-10-16", "valor_rs": 380.42},
    }))
    http = _Http()

    status = job.run_indicadores(s, paths, http, log)

    assert status["ok"] is True
    assert status["datagro"]["reaproveitado"] is True
    assert http.calls == []                            # DATAGRO não foi refeito
    assert job.indicadores_done_today(paths, s) is True


def test_job_force_refaz_datagro(tmp_path, monkeypatch):
    _patch_fontes(monkeypatch)
    _fixar_dia(monkeypatch)
    paths = _paths(tmp_path)
    (paths.indicadores_dir / "last_indicadores.json").write_text(json.dumps({
        "ok": True, "data_referencia": "2026-10-17", "datagro": {"ok": True},
    }))
    http = _Http()
    status = job.run_indicadores(_settings(), paths, http, log, force=True)
    assert status["ok"] is True
    assert "reaproveitado" not in status["datagro"]
    assert len(http.calls) == 1                        # DATAGRO refeito de verdade
