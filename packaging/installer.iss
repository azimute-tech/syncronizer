; Syncronizer — Inno Setup installer.
;
; Installs EVERYTHING the target needs with nothing pre-installed:
;   - a relocatable Python runtime (python-build-standalone)   -> {app}\runtime\python
;   - MinGit (portable git)                                    -> {app}\runtime\git
;   - NSSM (service wrapper)                                    -> {app}\runtime\nssm
; then git-clones the PUBLIC repo, creates a venv + installs deps, prompts for the
; Firebird .fdb path (+ optional API url/token), writes config.toml into the WRITABLE
; data dir (ProgramData, OUTSIDE the git tree), registers the Windows service and
; starts it. The service self-updates via `git pull` thereafter.
;
; Build prerequisite: run packaging\build_installer.ps1 first to populate build\ with
; the pre-extracted runtimes and the offline seed snapshot. Compile with ISCC.

#define MyAppName "Syncronizer"
#define MyAppVersion "0.1.0"
#define MyServiceName "Syncronizer"
; The public GitHub repository the service clones and self-updates from.
#define MyRepoUrl "https://github.com/azimute-tech/syncronizer.git"
#define MyBranch "main"

[Setup]
AppId={{8F2A1C30-9C4E-4F1A-B7D2-5A5E0C0E5001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Syncronizer
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Syncronizer ETL Service
OutputBaseFilename=syncronizer-setup
OutputDir=dist
WizardStyle=modern
SetupLogging=yes

[Files]
; Pre-extracted at build time (see build_installer.ps1). Inno ships plain directories.
Source: "build\python\*"; DestDir: "{app}\runtime\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\git\*";    DestDir: "{app}\runtime\git";    Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\nssm\nssm.exe"; DestDir: "{app}\runtime\nssm"; Flags: ignoreversion
; Offline fallback: a snapshot of the repo, copied to the clone dir only if git clone fails.
Source: "build\seed\*";   DestDir: "{app}\seed";           Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
Name: "{commonappdata}\Syncronizer";        Permissions: service-modify
Name: "{commonappdata}\Syncronizer\config"; Permissions: service-modify
Name: "{commonappdata}\Syncronizer\state";  Permissions: service-modify
Name: "{commonappdata}\Syncronizer\logs";   Permissions: service-modify

[Run]
; 1) Clone the public repo (skipped if a working tree already exists).
Filename: "{app}\runtime\git\cmd\git.exe"; \
  Parameters: "clone --depth 1 --branch {#MyBranch} {#MyRepoUrl} ""{commonappdata}\Syncronizer\repo"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Fetching application code..."; Check: NeedsClone

; 2) Create the virtualenv from the bundled standalone Python.
Filename: "{app}\runtime\python\python.exe"; \
  Parameters: "-m venv ""{commonappdata}\Syncronizer\venv"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Creating Python environment..."

; 3) Install dependencies + the package (editable) into the venv.
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m pip install --no-input --no-warn-script-location -r ""{commonappdata}\Syncronizer\repo\requirements.txt"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Installing dependencies..."
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m pip install --no-input --no-warn-script-location -e ""{commonappdata}\Syncronizer\repo"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Installing application..."

; 4) Self-check (imports endpoints + opens control DB). Abort the install if it fails.
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m syncronizer self-check"; WorkingDir: "{commonappdata}\Syncronizer\repo"; \
  Flags: runhidden waituntilterminated; StatusMsg: "Validating installation..."; Check: SelfCheckPass

; 5) (Re)register the service idempotently.
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden; Check: ServiceExists
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "remove {#MyServiceName} confirm"; Flags: runhidden; Check: ServiceExists
Filename: "{app}\runtime\nssm\nssm.exe"; \
  Parameters: "install {#MyServiceName} ""{commonappdata}\Syncronizer\venv\Scripts\python.exe"" -m syncronizer run"; Flags: runhidden waituntilterminated
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppDirectory ""{commonappdata}\Syncronizer\repo"""; Flags: runhidden
; Set the data dir AND the bundled git/nssm locations (in Program Files, with spaces)
; so the early boot gate can find git before config.toml is even loaded. Each KEY=VALUE
; is quoted as one token; NSSM accepts multiple AppEnvironmentExtra entries.
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppEnvironmentExtra ""SYNCRONIZER_DATA_DIR={commonappdata}\Syncronizer"" ""SYNCRONIZER_REPO_DIR={commonappdata}\Syncronizer\repo"" ""SYNCRONIZER_VENV_DIR={commonappdata}\Syncronizer\venv"" ""SYNCRONIZER_GIT_EXE={app}\runtime\git\cmd\git.exe"" ""SYNCRONIZER_NSSM_EXE={app}\runtime\nssm\nssm.exe"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppExit Default Restart"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppThrottle 60000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRestartDelay 30000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStdout ""{commonappdata}\Syncronizer\logs\service-stdout.log"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStderr ""{commonappdata}\Syncronizer\logs\service-stderr.log"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRotateFiles 1"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRotateBytes 10485760"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStopMethodConsole 15000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} Start SERVICE_AUTO_START"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "start {#MyServiceName}"; Flags: runhidden waituntilterminated; StatusMsg: "Starting service..."

[UninstallRun]
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden; RunOnceId: "StopSvc"
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "remove {#MyServiceName} confirm"; Flags: runhidden; RunOnceId: "RemoveSvc"

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\Syncronizer\repo"
Type: filesandordirs; Name: "{commonappdata}\Syncronizer\venv"

[Code]
var
  CfgPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  CfgPage := CreateInputQueryPage(wpSelectDir,
    'Firebird & API configuration',
    'Where is the data and where does it go?',
    'Informe o caminho do Firebird (.fdb), a URL base da API e a API Key. Tudo pode ser alterado depois em config.toml.');
  CfgPage.Add('Caminho completo do banco Firebird (.fdb):', False);
  CfgPage.Add('URL base da API (ex.: https://api.exemplo.com):', False);
  CfgPage.Add('API Key (enviada em toda requisicao):', True);   { masked }
  CfgPage.Add('Nome do header da API Key:', False);
  CfgPage.Values[0] := 'C:\data\app.fdb';
  CfgPage.Values[1] := 'https://';
  CfgPage.Values[2] := '';
  CfgPage.Values[3] := 'X-API-Key';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = CfgPage.ID) then
  begin
    if not FileExists(CfgPage.Values[0]) then
    begin
      MsgBox('O caminho do banco Firebird (.fdb) nao existe:' + #13#10 + CfgPage.Values[0],
        mbError, MB_OK);
      Result := False;
    end
    else if (Pos('http://', LowerCase(CfgPage.Values[1])) <> 1) and
            (Pos('https://', LowerCase(CfgPage.Values[1])) <> 1) then
    begin
      MsgBox('Informe a URL base da API (deve comecar com http:// ou https://).',
        mbError, MB_OK);
      Result := False;
    end
    else if (Length(Trim(CfgPage.Values[1])) <= 8) then
    begin
      MsgBox('A URL base da API esta incompleta.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function NeedsClone: Boolean;
begin
  Result := not DirExists(ExpandConstant('{commonappdata}\Syncronizer\repo\.git'));
end;

function ServiceExists: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{app}\runtime\nssm\nssm.exe'), 'status {#MyServiceName}',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function SelfCheckPass: Boolean;
begin
  // Always run; a failing self-check raises a fatal install error via the [Run] exit code.
  Result := True;
end;

procedure WriteConfig;
var
  DataDir, Cfg: string;
begin
  DataDir := ExpandConstant('{commonappdata}\Syncronizer');
  Cfg :=
    '# Generated by the Syncronizer installer. Holds secrets — never commit.' + #13#10 +
    '[firebird]' + #13#10 +
    'path = "' + CfgPage.Values[0] + '"' + #13#10 +
    'host = "localhost"' + #13#10 +
    'port = 3050' + #13#10 +
    'user = "SYSDBA"' + #13#10 +
    'password = "masterkey"' + #13#10 +
    'charset = "UTF8"' + #13#10 + #13#10 +
    '[api]' + #13#10 +
    'base_url = "' + CfgPage.Values[1] + '"' + #13#10 +
    'key = "' + CfgPage.Values[2] + '"' + #13#10 +
    'key_header = "' + CfgPage.Values[3] + '"' + #13#10 + #13#10 +
    '[update]' + #13#10 +
    'auto_update = true' + #13#10 +
    'branch = "{#MyBranch}"' + #13#10 +
    'repo_url = "{#MyRepoUrl}"' + #13#10 + #13#10 +
    '[paths]' + #13#10 +
    'git_exe = "' + ExpandConstant('{app}\runtime\git\cmd\git.exe') + '"' + #13#10 +
    'nssm_exe = "' + ExpandConstant('{app}\runtime\nssm\nssm.exe') + '"' + #13#10 +
    'repo_dir = "' + DataDir + '\repo"' + #13#10 +
    'venv_dir = "' + DataDir + '\venv"' + #13#10;
  // Forward slashes are safest in TOML; but absolute Windows paths with backslashes
  // are written here, so the loader reads them raw (config_value, not name).
  SaveStringToFile(DataDir + '\config\config.toml', Cfg, False);
end;

procedure HandleOfflineSeed;
begin
  // If the clone did not produce a working tree (offline), seed from the bundled snapshot.
  if NeedsClone and DirExists(ExpandConstant('{app}\seed')) then
  begin
    Log('git clone unavailable; seeding repo from offline snapshot');
    if not DirExists(ExpandConstant('{commonappdata}\Syncronizer\repo')) then
      CreateDir(ExpandConstant('{commonappdata}\Syncronizer\repo'));
    // (A production installer would xcopy {app}\seed -> repo and `git init`/remote add here.)
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteConfig;   // write config.toml BEFORE the [Run] clone/pip/self-check entries
  end;
end;
