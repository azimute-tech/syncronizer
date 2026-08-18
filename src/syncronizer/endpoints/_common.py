"""Shared helpers for the TGC endpoint modules.

Underscore-prefixed on purpose: :mod:`syncronizer.core.registry` skips modules whose
name starts with ``_``, so nothing here is ever mistaken for a feed.

Two things live here:

* value coercions (``req_str``, ``num``, ``iso_date``, ...) — every TGC feed maps the
  same Firebird types (INTEGER with scale, DATE, CHAR ``S``/``N``) into the same JSON
  shapes, so the rules are declared once;
* :class:`BatchEndpoint` — all TGC feeds POST an array under a wrapper key and get
  ``200 {inserted, updated, errors:[{<id>, message}]}`` back, reconciling per-item
  failures. Only the wrapper key and the identifier field differ per feed.

Note that :class:`BatchEndpoint` is abstract (it implements neither ``extract_spec``
nor ``transform``) and lives in a private module, so the registry cannot pick it up
even by accident.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time

from syncronizer.core.endpoint import Endpoint, SendResult

# A chip/SISBOV read out of a spreadsheet that Excel already destroyed: "9,63E+14".
# The value is unrecoverable (the original digits are gone), so it is dropped instead
# of being propagated as if it were an identifier. Verified in the client database:
# 66 rows, and this is the ONLY non-numeric chip value present.
_SCIENTIFIC_NOTATION = re.compile(r"^\d+[.,]\d+[eE][+-]?\d+$")


def req_str(value):
    """Required string: stringify + strip; '' if missing."""
    if value is None:
        return ""
    return str(value).strip()


def opt_str(value):
    """Optional string: trimmed, or None when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def opt_code(value):
    """Optional foreign key rendered as a string; 0 (TGC's "none" sentinel) -> None.

    TGC stores "no related row" as 0 in NOT NULL integer FKs (e.g. an inactive lote
    with ``CLL_CODCURRAL = 0``). There is no row with code 0 in the referenced tables,
    so forwarding it would create a dangling reference at the destination.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "0":
        return None
    return text


def num(value):
    """Numeric value as float (Firebird returns scaled integers as Decimal)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(value):
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def hhmm(value):
    """Firebird TIME -> texto ``HH:MM``; None quando ausente.

    O driver firebirdsql devolve uma coluna TIME como ``datetime.time`` (com
    microssegundos), e o contrato pede a hora como texto ``HH:MM`` — segundos e frações
    não têm significado nenhum num horário de batida digitado à mão. O fallback para
    ``str(value)[:5]`` cobre o caso de um driver devolver a hora já como texto.

    NÃO existe regra de zero aqui: ``00:00`` é encaminhado como qualquer outra hora
    (ver a nota de `batidas`, onde 100% das linhas da staging estão nesse default).
    """
    if value is None:
        return None
    if isinstance(value, (time, datetime)):
        return value.strftime("%H:%M")
    text = str(value).strip()[:5]
    return text or None


def flag_sn(value):
    """Firebird CHAR(1) 'S'/'N' -> bool. Anything else (incl. NULL) is False."""
    if value is None:
        return False
    return str(value).strip().upper() == "S"


def clean_id(value):
    """Optional animal identifier (chip / SISBOV), dropping known corruption.

    Scientific notation is never a valid identifier — it is what Excel leaves behind
    after widening a long digit string — so such values are discarded rather than
    forwarded as if they identified an animal.
    """
    text = opt_str(value)
    if text is None or _SCIENTIFIC_NOTATION.match(text):
        return None
    return text


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def watermark_param(watermark):
    """Coerce a stored watermark (always TEXT in SQLite) back to int when numeric.

    Firebird does cast '3950' to an INTEGER on its own, but binding the native type
    keeps the comparison (and the index choice) free of an implicit conversion.
    """
    if watermark is None:
        return None
    try:
        return int(str(watermark).strip())
    except (TypeError, ValueError):
        return watermark


class BatchEndpoint(Endpoint):
    """An endpoint that POSTs its rows as one array per request.

    Subclasses set ``payload_key`` (the JSON wrapper), ``record_key`` (the identifier
    field in the transformed record) and ``error_key`` (the same identifier as the API
    echoes it inside ``errors[]``). Rows the API rejects individually are marked failed
    and retried next cycle; the rest of the chunk is marked sent.

    There is no local quality gate anywhere: every extracted row is sent so the
    destination surfaces the problem. Corrections happen in TGC and the next cycle
    detects the changed row via ``row_hash`` and re-sends it.
    """

    payload_key = ""    # body is {"<payload_key>": [ ... ]}
    record_key = ""     # identifier field inside the transformed record
    error_key = ""      # identifier field inside each errors[] entry
    CHUNK = 200         # items per POST (under API max and rate limits)

    @staticmethod
    def _errors_by_id(resp, error_key):
        """Map identifier -> message from the API response, tolerating odd bodies.

        A 200 whose body is not the expected object must NOT fail the whole chunk
        (that would retry successfully-sent rows forever), so anything unparseable is
        read as "no per-item errors".
        """
        try:
            data = resp.json() if getattr(resp, "content", None) else {}
        except Exception:  # noqa: BLE001 - non-JSON body on a 2xx
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for err in data.get("errors") or []:
            if isinstance(err, dict):
                # ids round-trip through JSON as str or int depending on the API
                out[str(err.get(error_key))] = err.get("message")
        return out

    @staticmethod
    def _http_detail(exc):
        """Readable failure reason, including the API body when there is one."""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                return f"{resp.status_code}: {resp.text[:300]}"
            except Exception:  # noqa: BLE001
                pass
        return str(exc)

    def send(self, http, rows) -> SendResult:
        ok, failed = [], []
        parsed = []  # (pk, record)
        for row in rows:
            pk = row["pk"]
            try:
                parsed.append((pk, json.loads(row["payload"])))
            except Exception as exc:  # noqa: BLE001
                failed.append((pk, f"payload inválido: {exc}"))

        for chunk in chunks(parsed, self.CHUNK):
            items = [rec for _, rec in chunk]
            try:
                resp = http.request(
                    self.api_method, self.api_path, json={self.payload_key: items}
                )
            except Exception as exc:  # noqa: BLE001 - whole chunk retries next cycle
                detail = self._http_detail(exc)
                for pk, _ in chunk:
                    failed.append((pk, f"http: {detail}"))
                continue
            errors = self._errors_by_id(resp, self.error_key)
            for pk, rec in chunk:
                ident = str(rec.get(self.record_key))
                if ident in errors:
                    failed.append((pk, f"api: {errors[ident]}"))
                else:
                    ok.append(pk)
        return SendResult(ok, failed)
