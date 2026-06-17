"""Syncronizer — ETL sync agent (Firebird 2.5 -> SQLite control store -> HTTP API).

Runs as an always-on Windows service: every ``cycle_minutes`` it extracts from a
local Firebird database, upserts into a SQLite control DB (tracking what still
needs to be sent), and POSTs the pending rows to a remote API. Endpoints are code
modules auto-discovered from :mod:`syncronizer.endpoints`.

Keep this module import-time cheap and cross-platform: do NOT import Windows-only
packages or heavy submodules here.
"""

__version__ = "0.1.0"

# Schema version the CURRENT code expects in the control DB. The boot gate runs
# forward-only migrations until the DB reaches this; if the DB is newer (a code
# rollback), startup refuses rather than silently downgrading.
CODE_SCHEMA_VERSION = 1
