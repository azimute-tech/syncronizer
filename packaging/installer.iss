; Syncronizer — Inno Setup installer.
;
; Installs everything with nothing pre-installed (bundled Python + MinGit + NSSM),
; git-clones the public repo, creates a venv + installs deps, registers the Windows
; service and starts it. It does NOT ask for any settings: configuration is done
; afterwards in the local admin UI (http://127.0.0.1:8765). The service runs even with
; no config (ETL just waits) so the UI is always reachable.
;
; Build prerequisite: run packaging\build_installer.ps1 to populate build\ first.

#define MyAppName "Syncronizer"
#define MyAppVersion "0.1.0"
#define MyServiceName "Syncronizer"
#define MyRepoUrl "https://github.com/azimute-tech/syncronizer.git"
#define MyBranch "main"
#define MyPanelUrl "http://127.0.0.1:8765/"

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
Source: "build\python\*"; DestDir: "{app}\runtime\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\git\*";    DestDir: "{app}\runtime\git";    Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\nssm\nssm.exe"; DestDir: "{app}\runtime\nssm"; Flags: ignoreversion
Source: "build\seed\*";   DestDir: "{app}\seed";           Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Open the local control panel, and an easy uninstall — both in the Start Menu.
Name: "{autoprograms}\Syncronizer (Painel de controle)"; Filename: "{#MyPanelUrl}"
Name: "{autoprograms}\Desinstalar Syncronizer"; Filename: "{uninstallexe}"

[Dirs]
Name: "{commonappdata}\Syncronizer";        Permissions: service-modify
Name: "{commonappdata}\Syncronizer\config"; Permissions: service-modify
Name: "{commonappdata}\Syncronizer\state";  Permissions: service-modify
Name: "{commonappdata}\Syncronizer\logs";   Permissions: service-modify

[Run]
; 1) Clone the public repo (skipped if a working tree already exists).
Filename: "{app}\runtime\git\cmd\git.exe"; \
  Parameters: "clone --depth 1 --branch {#MyBranch} {#MyRepoUrl} ""{commonappdata}\Syncronizer\repo"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Baixando o aplicativo..."; Check: NeedsClone

; 2) Create the venv from the bundled standalone Python.
Filename: "{app}\runtime\python\python.exe"; \
  Parameters: "-m venv ""{commonappdata}\Syncronizer\venv"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Criando ambiente Python..."

; 3) Install dependencies + the package (editable).
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m pip install --no-input --no-warn-script-location -r ""{commonappdata}\Syncronizer\repo\requirements.txt"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Instalando dependências..."
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m pip install --no-input --no-warn-script-location -e ""{commonappdata}\Syncronizer\repo"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Instalando o aplicativo..."

; 4) Sanity check (imports endpoints + opens control DB). Firebird may be unconfigured.
Filename: "{commonappdata}\Syncronizer\venv\Scripts\python.exe"; \
  Parameters: "-m syncronizer self-check"; WorkingDir: "{commonappdata}\Syncronizer\repo"; \
  Flags: runhidden waituntilterminated; StatusMsg: "Validando instalação..."

; 5) (Re)register the service idempotently. NO AppEnvironmentExtra: the app hard-codes
;    the data dir to %PROGRAMDATA%\Syncronizer on Windows, so there is no env to mangle.
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden; Check: ServiceExists
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "remove {#MyServiceName} confirm"; Flags: runhidden; Check: ServiceExists
Filename: "{app}\runtime\nssm\nssm.exe"; \
  Parameters: "install {#MyServiceName} ""{commonappdata}\Syncronizer\venv\Scripts\python.exe"" -m syncronizer run"; Flags: runhidden waituntilterminated
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppDirectory ""{commonappdata}\Syncronizer\repo"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppExit Default Restart"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppThrottle 60000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRestartDelay 30000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStopMethodConsole 15000"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStdout ""{commonappdata}\Syncronizer\logs\service-stdout.log"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppStderr ""{commonappdata}\Syncronizer\logs\service-stderr.log"""; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRotateFiles 1"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} AppRotateBytes 10485760"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "set {#MyServiceName} Start SERVICE_AUTO_START"; Flags: runhidden
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "start {#MyServiceName}"; Flags: runhidden waituntilterminated; StatusMsg: "Iniciando o serviço..."

; 6) Open the control panel so the user can configure it.
Filename: "{#MyPanelUrl}"; Flags: shellexec postinstall nowait skipifsilent; Description: "Abrir o painel de controle do Syncronizer"

[UninstallRun]
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden; RunOnceId: "StopSvc"
Filename: "{app}\runtime\nssm\nssm.exe"; Parameters: "remove {#MyServiceName} confirm"; Flags: runhidden; RunOnceId: "RemoveSvc"

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\Syncronizer\repo"
Type: filesandordirs; Name: "{commonappdata}\Syncronizer\venv"

[Code]
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

// Runs BEFORE any files are copied: stop + remove the running service so its python
// releases file handles, otherwise overwriting runtime\python\python.exe fails with
// "Access denied / DeleteFile failed; code 5" when reinstalling over a running install.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Rc: Integer;
  Nssm: String;
begin
  Result := '';
  Nssm := ExpandConstant('{app}\runtime\nssm\nssm.exe');
  if FileExists(Nssm) then
  begin
    Exec(Nssm, 'stop {#MyServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Rc);
    Exec(Nssm, 'remove {#MyServiceName} confirm', '', SW_HIDE, ewWaitUntilTerminated, Rc);
    Exec(ExpandConstant('{cmd}'), '/c timeout /t 4 /nobreak', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  end;
end;

// On uninstall, after the service is removed, offer to wipe the data dir so the next
// install starts from zero (e.g. migrating dev -> prod).
procedure CurUninstallStepChanged(CurStep: TUninstallStep);
begin
  if CurStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{commonappdata}\Syncronizer')) then
    begin
      if MsgBox('Remover tambem todos os dados e configuracoes ' +
                '(control.db, logs e config.toml em ProgramData)?' + #13#10 +
                'Escolha Sim para comecar do zero (ex.: migrar de dev para prod).',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(ExpandConstant('{commonappdata}\Syncronizer'), True, True, True);
    end;
  end;
end;
