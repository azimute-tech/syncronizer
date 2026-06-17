"""TGC `animais` endpoint — sync the herd from Firebird (CAD_ANIMAL) to AgroDB.

Source: Firebird 2.5 `CAD_ANIMAL` (+ CAD_CATEGORIA, CAD_CURRAL).
Target: POST /api/integracoes/tgc/animais  body {"animais": [ ... ]} (1..500 itens).
Auth:   X-API-Key (or Authorization: Bearer) = per-farm TGC token. farm_id comes from
        the token server-side and is NOT sent in the body.

The API validates the WHOLE batch with zod (a single invalid item 400s the batch), then
upserts and returns 200 {inserted, updated, errors:[{cod_animal, message}]}. So send()
here validates each animal locally first (drops invalid ones so they never sink a good
batch), POSTs in chunks, and reconciles per-item failures by COD_ANIMAL.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from syncronizer.core.endpoint import Endpoint, SendResult
from syncronizer.core.extract import ExtractContext, ExtractSpec

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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

    # --- local validation mirroring the API's zod rules ---
    @staticmethod
    def _validate(a: dict):
        for field in ("COD_ANIMAL", "LOTE_ENTRADA", "LOTE_ATUAL", "USUARIO"):
            if not a.get(field):
                return f"{field} vazio"
        peso = a.get("PESO_BALANCINHA")
        if not isinstance(peso, (int, float)) or peso <= 0:
            return f"PESO_BALANCINHA inválido ({peso})"
        rc = a.get("RC_ENTRADA")
        if not isinstance(rc, (int, float)) or rc <= 0 or rc > 100:
            return f"RC_ENTRADA inválido ({rc})"
        idade = a.get("IDADE")
        if not isinstance(idade, int) or idade < 0 or idade > 480:
            return f"IDADE inválida ({idade})"
        data = a.get("DATA_ENTRADA")
        if not isinstance(data, str) or not _DATE_RE.match(data):
            return f"DATA_ENTRADA inválida ({data})"
        if data > datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            return f"DATA_ENTRADA no futuro ({data})"
        return None

    def send(self, http, rows) -> SendResult:
        ok, failed = [], []
        valid = []  # (pk, animal)
        for row in rows:
            pk = row["pk"]
            try:
                animal = json.loads(row["payload"])
            except Exception as exc:  # noqa: BLE001
                failed.append((pk, f"payload inválido: {exc}"))
                continue
            err = self._validate(animal)
            if err:
                failed.append((pk, f"validação local: {err}"))
            else:
                valid.append((pk, animal))

        for chunk in _chunks(valid, self.CHUNK):
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
                for pk, _ in chunk:
                    failed.append((pk, f"http: {exc}"))
        return SendResult(ok, failed)
