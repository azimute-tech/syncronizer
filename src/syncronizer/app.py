"""The Application object: owns settings, paths, the control store, the HTTP
client and the discovered endpoint registry; exposes ``run_cycle``.
"""
from __future__ import annotations

import logging

from .config import Settings, load_settings
from .core import orchestrator, registry
from .core.migrations import run_migrations
from .db.firebird import FirebirdClient
from .db.store import ControlStore, now_iso
from .http.client import HttpClient
from .logging_setup import summary_line
from .paths import build_paths

log = logging.getLogger("syncronizer")


class Application:
    def __init__(self, settings: Settings = None):
        self.settings = settings or load_settings()
        self.paths = build_paths(self.settings)
        self.paths.ensure_dirs()

        self.store = ControlStore(self.paths.control_db, log=log)
        run_migrations(self.store, log=log)

        self.http = HttpClient(
            self.settings.api_base_url, self.settings.api_token,
            self.settings.api_timeout, self.settings.api_max_retries,
            api_key=self.settings.api_key, api_key_header=self.settings.api_key_header,
        )
        self.endpoints = registry.discover(log=log)
        log.info(
            "discovered %d endpoint(s): %s",
            len(self.endpoints),
            ", ".join(e.name for e in self.endpoints) or "(none)",
        )
        self._restart_requested = False
        self._had_successful_cycle = False
        self.last_cycle_at = None
        self.last_fb_ok = False

    def run_cycle(self):
        log.info("CYCLE START")
        fb = FirebirdClient(self.settings, log=log)
        try:
            stats = orchestrator.run_cycle(
                self.endpoints, fb, self.store, self.http, log, self.settings.batch_size
            )
        finally:
            fb.close()
        totals = stats.totals()
        self.last_cycle_at = now_iso()
        self.last_fb_ok = stats.firebird_available
        log.info("CYCLE END %s", summary_line(fb=stats.firebird_available, **totals))
        # Per-endpoint backlog/progress so the log shows how much is left to send.
        for ep in self.endpoints:
            c = self.store.endpoint_counts(ep.name)
            if c["total"]:
                pct = c["sent"] * 100 // c["total"]
                log.info("  PROGRESSO %s: enviados=%d/%d (%d%%) pendentes=%d deletados=%d",
                         ep.name, c["sent"], c["total"], pct, c["pending"], c["deleted"])
        # A cycle that completed with no per-endpoint exceptions means this commit's
        # code runs (Firebird/API being down are environmental, not code defects).
        if totals["errors"] == 0:
            self._had_successful_cycle = True
        try:
            self.store.checkpoint()
        except Exception:  # noqa: BLE001
            pass
        return stats

    @property
    def had_successful_cycle(self) -> bool:
        return self._had_successful_cycle

    # -- self-update restart signalling --
    def request_restart(self) -> None:
        self._restart_requested = True

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def close(self) -> None:
        try:
            self.http.close()
        finally:
            self.store.close()
