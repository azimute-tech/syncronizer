"""Auto-discovery of endpoint modules.

Walks the :mod:`syncronizer.endpoints` package, importing every module whose name
does NOT start with ``_`` (so ``_template`` is skipped), collecting concrete
:class:`Endpoint` subclasses defined in that module. A module that fails to import
is logged and skipped so one broken endpoint never crashes the service.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import List

from .endpoint import Endpoint


def discover(log=None, package=None) -> List[Endpoint]:
    """Discover endpoint instances from ``package`` (default ``syncronizer.endpoints``)."""
    if package is None:
        from .. import endpoints as package

    found: List[Endpoint] = []
    for modinfo in pkgutil.iter_modules(package.__path__):
        if modinfo.name.startswith("_"):
            continue
        full = f"{package.__name__}.{modinfo.name}"
        try:
            module = importlib.import_module(full)
        except Exception as exc:  # noqa: BLE001 - isolate a broken endpoint file
            if log:
                log.error("skipping endpoint module %s: %s", full, exc)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Endpoint)
                and obj is not Endpoint
                and obj.__module__ == module.__name__
                and not inspect.isabstract(obj)
            ):
                try:
                    found.append(obj())
                except Exception as exc:  # noqa: BLE001
                    if log:
                        log.error("cannot instantiate endpoint %s: %s", obj.__name__, exc)

    found.sort(key=lambda ep: (getattr(ep, "order", 100), ep.name))
    return found
