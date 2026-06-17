"""Logging configuration for a headless, always-on service.

A rotating UTF-8 file handler is the baseline (the service has no console); a
console handler is added only when stderr is a TTY (development). Uncaught
exceptions are routed to the log before the process exits so NSSM restarts leave
a trace.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def setup_logging(log_file: Path, level: str = "INFO",
                  max_bytes: int = 10 * 1024 * 1024, backup_count: int = 7) -> logging.Logger:
    """Configure root logging once; return the package logger."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return logging.getLogger("syncronizer")

    root.setLevel(level.upper())
    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    logging.captureWarnings(True)

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("syncronizer").critical(
            "uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _excepthook
    _configured = True
    return logging.getLogger("syncronizer")


def summary_line(**fields) -> str:
    """Render a structured ``key=value`` one-liner for cycle summaries."""
    parts = []
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    return " ".join(parts)
