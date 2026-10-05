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

Nightly jobs (local time, same `tz_offset_hours`):

- **Indicadores (CEPEA boi gordo)** — `[indicadores] enabled`, **ligado por padrão**
  (20:30). Envia o Indicador do Boi Gordo CEPEA/B3 para `POST /api/integracoes/indicadores`
  reusando o auth de `[api]`; desligue só por decisão explícita (painel ou config.toml).
- **Backup** — `[backup] enabled`, desligado por padrão.

`config.toml` carries a `config_version`. Files without it (v1) are migrated once at
boot by `configio.migrate_config`: v2 turns `[indicadores] enabled = false` back on,
since before v2 that value could only be the old default (the panel had no such field).
The admin panel stamps the current version on every save, so later choices are kept.

## Aviso de fim de ciclo (`ciclo-concluido`)

Ao fim de todo ciclo que **leu o Firebird**, o serviço faz
`POST /api/integracoes/tgc/ciclo-concluido` (mesma URL base e mesmo auth de `[api]`):

```json
{ "ciclo_id": "…", "iniciado_em": "…", "concluido_em": "…",
  "recursos": { "<endpoint>": { "enviados": 0, "falhas": 0 } } }
```

A API responde `{ versao, recalculado }` (logado em INFO) e recalcula os derivados da
fazenda uma vez por ciclo, em vez de a cada lote recebido.

- **Sempre avisa**, mesmo num ciclo sem mudança: é um POST pequeno e deixa a API
  recalcular uma fazenda que ficou pendente. Quem decide recalcular é a API.
- **Sem Firebird, sem aviso**: não houve carga nova.
- **Repete até confirmar**: o aviso é gravado no `control.db` (`_sync_meta`,
  chave `ciclo_concluido_pendente`) antes de enviar; 3 tentativas no ciclo (esperas de
  2 s e 5 s) para rede/5xx/408/429; timeout não repete no ciclo. O que sobrar é reenviado
  no início e no fim dos próximos ciclos — inclusive após reiniciar o serviço. Avisos
  pendentes se fundem num só (último `ciclo_id`, `iniciado_em` mais antigo, contadores
  somados), então uma queda de internet não vira uma rajada de recálculos.
- **404** (API antiga, sem a rota): um WARNING, descarta o pendente e para de avisar até
  o serviço reiniciar. Outro 4xx (400/401/403): WARNING e descarta (repetir não resolve).
  Em ambos a API ainda recalcula sozinha a fazenda pendente há mais de 15 min.

### Intervalo do ciclo

É só configuração: `cycle_minutes` em `[runtime]` do `config.toml` (ou o campo
"Intervalo do ciclo (min)" no painel), seguido de **Reiniciar** no painel — o agendador
lê o valor no boot. Para 15 min: `cycle_minutes = 15`. O padrão continua 10.

## Add an endpoint

Copy `src/syncronizer/endpoints/_template.py` to `endpoints/<name>.py`, fill in
`name` / `primary_key` / `api_path`, replace the inert `SELECT FIRST 0 ...` query
with the real Firebird SQL, map columns in `transform()`, commit and push. Machines
pick it up on the next git-sync tick.

## Get the Windows installer (no local Windows needed)

The `setup.exe` is built in the cloud by GitHub Actions on a Windows runner
(`.github/workflows/build-installer.yml`). You never need a local Windows machine:

- **Any build:** open the repo's **Actions** tab → the latest `build-installer` run →
  download the **`syncronizer-setup`** artifact.
- **Release build:** push a tag `vX.Y.Z` → a GitHub **Release** is created with
  `syncronizer-setup.exe` attached.
- **Manual:** Actions tab → `build-installer` → **Run workflow**.

The installer bundles a relocatable Python + MinGit + NSSM, clones this repo, registers
the Windows service and starts it. The target machine needs nothing pre-installed.

To build locally instead (on a Windows box with Inno Setup 6):
`pwsh packaging/build_installer.ps1` → `packaging/dist/syncronizer-setup.exe`.

## Admin panel (configure after install)

The installer asks for **nothing** — it just installs and starts the service. All
configuration is done in a **local web panel** the service hosts at
**http://127.0.0.1:8765** (Start Menu → *Syncronizer (Painel de controle)*). The panel:

- edits every setting (Firebird path/credentials, API URL/key, intervals…) — paths
  are normalized automatically, so `\` vs `/` and TOML escaping are never a problem;
- shows the logs and the live status;
- lists endpoints with **enable/disable** toggles and a **clear-and-resync** button;
- has a **Restart** button to apply config changes.

The service runs even with no configuration yet (ETL just waits), so the panel is
always reachable. The panel binds to localhost only.
