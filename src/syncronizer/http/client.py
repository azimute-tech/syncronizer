"""Shared HTTP client for endpoint sends.

Centralizes base URL, timeout, auth header and transport-level retry/backoff.
Raises on non-2xx (``raise_for_status``) so the orchestrator marks the row failed
(it stays ``sent=0`` and retries next cycle).
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 is a requests dependency
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None


def _build_retry(max_retries: int):
    if Retry is None:
        return None
    forcelist = (429, 500, 502, 503, 504)
    methods = frozenset(["GET", "POST", "PUT", "DELETE", "PATCH"])
    try:  # urllib3 >= 1.26
        return Retry(total=max_retries, backoff_factor=0.5,
                     status_forcelist=forcelist, allowed_methods=methods)
    except TypeError:  # pragma: no cover - older urllib3
        return Retry(total=max_retries, backoff_factor=0.5,
                     status_forcelist=forcelist, method_whitelist=methods)


class HttpClient:
    def __init__(self, base_url: str = "", token: str = "",
                 timeout: int = 30, max_retries: int = 3,
                 api_key: str = "", api_key_header: str = "X-API-Key"):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"
        if api_key and api_key_header:
            self.session.headers[api_key_header] = api_key
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        retry = _build_retry(max_retries)
        if retry is not None:
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def request(self, method: str, path: str, json=None, **kwargs):
        resp = self.session.request(
            method, self._url(path), json=json, timeout=self.timeout, **kwargs
        )
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self.session.close()
