"""TGC `animais` endpoint — sync the herd from Firebird (CAD_ANIMAL) to AgroDB.

Source: Firebird 2.5 `CAD_ANIMAL` (+ CAD_CATEGORIA, CAD_CURRAL).
Target: POST /api/integracoes/tgc/animais  body {"animais": [ ... ]} (1..500 itens).
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

The API upserts and returns 200 {inserted, updated, errors:[{cod_animal, message}]}. So
send() here POSTs in chunks and reconciles per-item failures by COD_ANIMAL.

Data-quality problems (RC_ENTRADA > 100, duplicate SISBOV, ...) are NOT filtered out
locally — every animal is sent so the destination surfaces the issue. Corrections happen
in the source (TGC); the next sync detects the changed row (by COD_ANIMAL within the farm)
and re-sends it with the corrected value.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from syncronizer.core.endpoint import Endpoint, SendResult
from syncronizer.core.extract import ExtractContext, ExtractSpec


def _req_str(value):
    """Required string: stringify + strip; '' if missing."""
    if value is None:
        return ""
    return str(value).strip()


def _opt_str(value):
    """Optional string: trimmed, or None when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _saida(value):
    text = _opt_str(value)
    if text is None or text.upper() == "NENHUM":
        return None
    return text


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_date(value):
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class AnimaisEndpoint(Endpoint):
    name = "animais"                 # -> control table ep_animais
    primary_key = "COD_ANIMAL"       # unique within a farm (one syncronizer = one farm/token)
    order = 10
    api_path = "/api/integracoes/tgc/animais"
    api_method = "POST"

    # Full scan + row_hash change detection (only changed/new animals are re-sent).
    # To scope it (e.g. active head only) add a WHERE to _BASE_SQL.
    incremental_column = None
    reconcile_deletes = False        # exits are tracked via SAIDA, not row deletion

    CHUNK = 200                      # animals per POST (<= API max 500, under rate limits)

    _BASE_SQL = """
        SELECT
            a.CA_CODIGO          AS COD_ANIMAL,
            a.CA_CODLOTE_ENTRADA AS LOTE_ENTRADA,
            a.CA_CODLOTE         AS LOTE_ATUAL,
            cur.CC_NOME          AS CURRAL_ATUAL,
            a.CA_PESO_ENT        AS PESO_BALANCINHA,
            a.CA_RC_ENTRADA      AS RC_ENTRADA,
            a.CA_IDADE           AS IDADE,
            a.CA_DATAENT         AS DATA_ENTRADA,
            a.CA_USUARIO         AS USUARIO,
            a.CA_NUMCONTRATO     AS NUM_CONTRATO,
            cat.CCAT_NOME        AS CATEGORIA,
            a.CA_SISBOV          AS SISBOV,
            a.CA_CHIP            AS CHIP,
            a.CA_SAIDA           AS SAIDA
        FROM CAD_ANIMAL a
        LEFT JOIN CAD_CATEGORIA cat ON cat.CCAT_CODIGO = a.CA_CODCATEGORIA
        LEFT JOIN CAD_CURRAL    cur ON cur.CC_CODIGO   = a.CA_ULTIMO_CURRAL
        ORDER BY a.CA_CODIGO
    """

    def extract_spec(self, ctx: ExtractContext) -> ExtractSpec:
        return ExtractSpec(sql=self._BASE_SQL, params=())

    def transform(self, row: dict) -> dict:
        lote_entrada = _req_str(row.get("LOTE_ENTRADA"))
        record = {
            "COD_ANIMAL": _req_str(row.get("COD_ANIMAL")),
            "LOTE_ENTRADA": lote_entrada,
            # live lote; if the source has no realocação, fall back to the entry lote
            "LOTE_ATUAL": _req_str(row.get("LOTE_ATUAL")) or lote_entrada,
            "CURRAL_ATUAL": _opt_str(row.get("CURRAL_ATUAL")),
            "PESO_BALANCINHA": _num(row.get("PESO_BALANCINHA")),
            "RC_ENTRADA": _num(row.get("RC_ENTRADA")),
            "IDADE": _int(row.get("IDADE")),
            "DATA_ENTRADA": _iso_date(row.get("DATA_ENTRADA")),
            "USUARIO": _req_str(row.get("USUARIO")),
            "NUM_CONTRATO": _opt_str(row.get("NUM_CONTRATO")),
            "CATEGORIA": _opt_str(row.get("CATEGORIA")),
        }
        # optional fields: include only when present (mirrors the validated sample)
        for key, val in (("SISBOV", _opt_str(row.get("SISBOV"))),
                         ("CHIP", _opt_str(row.get("CHIP"))),
                         ("SAIDA", _saida(row.get("SAIDA")))):
            if val is not None:
                record[key] = val
        return record

    def send(self, http, rows) -> SendResult:
        ok, failed = [], []
        parsed = []  # (pk, animal) — every animal is sent; no local quality gate
        for row in rows:
            pk = row["pk"]
            try:
                parsed.append((pk, json.loads(row["payload"])))
            except Exception as exc:  # noqa: BLE001
                failed.append((pk, f"payload inválido: {exc}"))

        for chunk in _chunks(parsed, self.CHUNK):
            items = [a for _, a in chunk]
            try:
                resp = http.request(self.api_method, self.api_path, json={"animais": items})
                data = resp.json() if resp.content else {}
                err_by_cod = {e.get("cod_animal"): e.get("message")
                              for e in (data.get("errors") or [])}
                for pk, animal in chunk:
                    cod = animal.get("COD_ANIMAL")
                    if cod in err_by_cod:
                        failed.append((pk, f"api: {err_by_cod[cod]}"))
                    else:
                        ok.append(pk)
            except Exception as exc:  # noqa: BLE001 - whole chunk retries next cycle
                # capture the API response body so the reason is actionable
                # (status + e.g. which field/animal the API rejected, 401, 429, ...)
                detail = str(exc)
                resp = getattr(exc, "response", None)
                if resp is not None:
                    try:
                        detail = f"{resp.status_code}: {resp.text[:300]}"
                    except Exception:  # noqa: BLE001
                        pass
                for pk, _ in chunk:
                    failed.append((pk, f"http: {detail}"))
        return SendResult(ok, failed)
