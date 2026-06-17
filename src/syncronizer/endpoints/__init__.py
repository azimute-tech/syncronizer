"""Endpoint modules, auto-discovered at startup.

Every module here whose name does NOT start with ``_`` is imported and scanned for
concrete :class:`syncronizer.core.endpoint.Endpoint` subclasses. There is no manual
registration list — adding an endpoint is dropping a file in this directory.
"""
