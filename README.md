# Syncronizer

ETL sync agent that runs as an **always-on Windows service**. Every 10 minutes it:

1. **Extracts** rows from a local **Firebird 2.5** database (pure-Python `firebirdsql` driver — no native client needed),
2. **Upserts** them into a local **SQLite** control database, marking new/changed rows `sent=0` (and rows that vanished from the source `deleted=1`),
3. **POSTs** the pending rows to a remote **HTTP API**.

Each *endpoint* is one Python module under `src/syncronizer/endpoints/`, auto-discovered at startup. Adding an endpoint = add one file (Firebird query + transform + API path); the base handles connection, change-detection, the sent/deleted flags, retries and HTTP.

The project is distributed as a **public GitHub repo**. A Windows installer (Inno Setup) bundles Python + git + NSSM, clones the repo, registers the service, and starts it. The running service **`git pull`s itself** on a schedule, so pushing a new endpoint to GitHub rolls it out to every machine without reinstalling.

## Layout on the target machine

```
C:\Program Files\Syncronizer\runtime\{python,git,nssm}   # bundled binaries (read-only)
C:\ProgramData\Syncronizer\
  repo\     git clone of THIS repo (the app code; `git pull` updates it)
  venv\     virtualenv (deps; `pip install -e repo`)
  config\config.toml   secrets: .fdb path, SYSDBA/masterkey, API url/token  (NEVER in the repo)
  state\    control.db (+ -wal/-shm), last_applied_commit, last_good_commit, boot_attempts, quarantine.json
  logs\     syncronizer.log + NSSM stdout/stderr
```

The app locates its writable data dir via the `SYNCRONIZER_DATA_DIR` env var (set by the installer/NSSM), so the service's `cwd` never matters.

## Develop on macOS / Linux

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export SYNCRONIZER_DATA_DIR="$PWD/data"      # writable dev data dir
export SYNC_FIREBIRD_PATH="/path/to/app.fdb" # required; or set [firebird].path in config.toml

python -m syncronizer print-config   # show resolved settings
python -m syncronizer self-check      # import endpoints + open DBs
python -m syncronizer run-once        # one ETL+SEND cycle
python -m syncronizer run             # always-on scheduler loop (Ctrl+C = graceful stop)

pytest                                # unit tests (no Firebird required)
```

`config.toml` is read from `$SYNCRONIZER_DATA_DIR/config/config.toml`; env vars
(`SYNC_*`) override it. See `config.toml.example`.

## Add an endpoint

Copy `src/syncronizer/endpoints/_template.py` to `endpoints/<name>.py`, fill in
`name` / `primary_key` / `api_path`, replace the inert `SELECT FIRST 0 ...` query
with the real Firebird SQL, map columns in `transform()`, commit and push. Machines
pick it up on the next git-sync tick.

## Build the Windows installer

On a Windows build box, run `packaging/build_installer.ps1` (downloads the
relocatable Python + MinGit + NSSM and compiles `setup.exe` with Inno Setup).
See `packaging/installer.iss`.
