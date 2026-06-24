"""Localhost admin HTTP server (stdlib only).

Serves the single-page UI and a small JSON API to read/write config, view logs,
list/enable/disable/reset endpoints, and restart the service. Bound to 127.0.0.1 —
it is an on-machine admin panel, not a network service.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__, configio
from .page import PAGE

log = logging.getLogger("syncronizer.web")


def _log_tail(path, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:]) or "(log vazio)"
    except FileNotFoundError:
        return "(sem logs ainda)"
    except Exception as exc:  # noqa: BLE001
        return f"(erro lendo log: {exc})"


def build_state(app) -> dict:
    s = app.settings
    effective = json.loads(s.model_dump_json())
    # The config form must reflect what's SAVED in config.toml (the persisted, editable
    # state) — NOT the in-memory settings loaded at service start. app.settings only
    # reloads config.toml on restart, so without this a value saved without restarting
    # appears to "revert" on the next form render. Saved values override startup defaults.
    form_values = {**effective, **configio.read_flat(app.paths.config_file)}
    totals = {"total": 0, "sent": 0, "pending": 0, "deleted": 0}
    endpoints = []
    for ep in app.endpoints:
        counts = app.store.endpoint_counts(ep.name)
        for k in totals:
            totals[k] += counts.get(k, 0)
        endpoints.append({
            "name": ep.name,
            "api_path": getattr(ep, "api_path", ""),
            "enabled": app.store.is_endpoint_enabled(ep.name),
            "counts": counts,
            "last_send_at": app.store.last_endpoint_state(ep.name)["last_send_at"],
        })
    status = {
        "version": __version__,
        "service": "rodando",
        "last_cycle": getattr(app, "last_cycle_at", None),
        "firebird_configured": bool(s.firebird_path),
        "firebird_ok": bool(getattr(app, "last_fb_ok", False)),
        "api_configured": bool(s.api_base_url and (s.api_key or s.api_token)),
        "totals": totals,
    }
    return {"status": status, "config": configio.form_model(form_values), "endpoints": endpoints}


def _make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr logging
            pass

        def _send(self, code, payload, ctype="application/json"):
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                data = str(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw or b"{}")
            except Exception:  # noqa: BLE001
                return {}

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html")
            if u.path == "/api/state":
                return self._send(200, build_state(app))
            if u.path == "/api/logs":
                n = int((parse_qs(u.query).get("n", ["200"])[0]) or 200)
                return self._send(200, _log_tail(app.paths.log_file, n), "text/plain")
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                if u.path == "/api/config":
                    configio.save_config(app.paths.config_file, self._json_body())
                    return self._send(200, {"ok": True})
                if u.path == "/api/endpoint":
                    body = self._json_body()
                    name, action = body.get("name"), body.get("action")
                    names = {ep.name for ep in app.endpoints}
                    if name not in names:
                        return self._send(404, {"error": "endpoint desconhecido"})
                    if action == "enable":
                        app.store.set_endpoint_enabled(name, True)
                    elif action == "disable":
                        app.store.set_endpoint_enabled(name, False)
                    elif action == "reset":
                        app.store.reset_endpoint(name)
                    else:
                        return self._send(400, {"error": "acao invalida"})
                    return self._send(200, {"ok": True})
                if u.path == "/api/restart":
                    app.request_restart()
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                log.exception("admin API error")
                return self._send(500, {"error": str(exc)})

    return Handler


def start(app, logger=None):
    """Start the admin server in a daemon thread. Returns the server or None."""
    s = app.settings
    out = logger or log
    if not getattr(s, "admin_enabled", True):
        return None
    try:
        srv = ThreadingHTTPServer((s.admin_host, s.admin_port), _make_handler(app))
    except Exception as exc:  # noqa: BLE001
        out.error("admin UI não pôde iniciar em %s:%s: %s", s.admin_host, s.admin_port, exc)
        return None
    threading.Thread(target=srv.serve_forever, name="admin-web", daemon=True).start()
    out.info("admin UI em http://%s:%s", s.admin_host, s.admin_port)
    return srv
