# `syncronizer self-check`

A fast, side-effect-light validation used by the installer (and available for
diagnostics). It is the gate that prevents registering/applying broken code.

What it validates:

1. **Endpoint discovery** — imports every `endpoints/*.py` module (skipping `_`-prefixed)
   and instantiates the `Endpoint` subclasses. A broken endpoint module is reported.
2. **Control DB** — opens `state/control.db`, applies pragmas, runs forward-only
   migrations, prints the resulting `schema_version`. A schema *newer* than the code
   (a rollback) makes this fail.
3. **Firebird** — if `firebird.path` is configured, attempts a connection and reports
   reachability.
4. **Backup** — only when `[backup].enabled` is true: validates that `gbak` is present
   (configured path or auto-discovered), that the temp dir is writable (probe write),
   and that the API is configured (`base_url` + key/token). Any of these missing makes
   the self-check FAIL — the backup cannot run without them (a missing `gbak` never
   falls back to a raw `.fdb` copy).

Exit codes:

- `0` — endpoints import and the control DB opens. Firebird being unreachable is a
  WARNING by default (the service retries every cycle).
- `1` — endpoints failed to import, the control DB could not be opened/migrated,
  (with `--require-firebird`) Firebird was unreachable, or — with `[backup].enabled` —
  `gbak`/temp dir/API are not ready for the nightly backup.

Usage:

```
python -m syncronizer self-check                  # imports + control DB (Firebird soft)
python -m syncronizer self-check --require-firebird  # also require a live Firebird
```

The self-updater uses the same idea: after a `git pull` that changes code, a passing
self-check is the precondition for committing the new commit as "good" and restarting.
