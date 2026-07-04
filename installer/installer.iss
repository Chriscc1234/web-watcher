; Web Watcher — Inno Setup installer
; ---------------------------------------------------------------------------
; Ships a self-contained Python runtime (build\runtime\python) + the app code
; (build\runtime\app) so the end user needs NOTHING pre-installed except, at
; first run, Ollama + the local models (provisioned by provision.py).
;
; PER-USER install to %LOCALAPPDATA%\Programs\WebWatcher (PrivilegesRequired=lowest):
;   • no admin prompt, and
;   • the in-app auto-updater (which stages code swaps into the app folder) can
;     write there — a Program Files install would need admin for every update.
;
; User DATA is NOT here — it lives in %LOCALAPPDATA%\WebWatcher (see web_watcher\paths.py),
; so uninstalling/reinstalling the app never touches watches, results, or saved logins.
;
; Build:  iscc installer\installer.iss   (after `python build_runtime.py`)
; Output: installer\Output\WebWatcher-Setup-<version>.exe
; ---------------------------------------------------------------------------

#define AppName "Web Watcher"
#define AppPublisher "Web Watcher"
; Version is passed in by build_installer.py via /DAppVersion=...; fall back if run by hand.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7E1D9A4C-2F5B-4C8E-9A31-0B2C3D4E5F60}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install — no admin, self-update friendly.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\WebWatcher
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\app\web_watcher\dashboard\static\icon.ico
OutputDir=Output
OutputBaseFilename=WebWatcher-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The bundled runtime is 64-bit only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The self-contained Python runtime (python.exe, Lib, site-packages with all deps).
Source: "..\build\runtime\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
; The app code (web_watcher\, launcher.py, provision.py, …).
Source: "..\build\runtime\app\*";    DestDir: "{app}\app";    Excludes: "*.pyc,__pycache__\*,tests\*,_smoke_data\*"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Windowless launch: pythonw.exe running launcher.py (which applies staged updates then starts the app).
; IconFilename gives the shortcuts the app's own magnifying-glass icon (not the generic python one).
Name: "{group}\{#AppName}";        Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\web_watcher\dashboard\static\icon.ico"; AppUserModelID: "WebWatcher.App"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}";  Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\web_watcher\dashboard\static\icon.ico"; AppUserModelID: "WebWatcher.App"; Tasks: desktopicon

[Run]
; First-run provisioning: ensure Ollama is present + pull the local models (shows its own
; console window with progress). Uses the console python.exe so the user sees progress.
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\provision.py"""; WorkingDir: "{app}\app"; StatusMsg: "Setting up local AI models (first run — this downloads several GB)…"; Flags: postinstall
; Offer to launch the app after install.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Description: "Launch {#AppName} now"; Flags: postinstall nowait skipifsilent

; NOTE: no [UninstallRun] to remove shortcuts — Inno automatically removes the [Icons] it
; created. We deliberately do NOT run uninstall.py here: it deletes "Web Watcher" shortcuts by
; NAME from the user's Desktop/Start Menu, which could clobber a *different* install's shortcuts.
; Data deletion is handled by the opt-in prompt in [Code] below.

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nWeb Watcher bundles its own Python, so nothing else is required up front. On first launch it will download the local AI models (several GB, one time) via Ollama — an internet connection is needed for that step, but the app runs fully offline afterward.

[Code]
// On uninstall, offer to also delete the user's data. It lives OUTSIDE the app folder
// (%LOCALAPPDATA%\WebWatcher — watches, results, saved logins, logs, DB), so a normal
// uninstall keeps it for a future reinstall. Give the user a clear one-time choice.
// In a silent uninstall we never prompt and always KEEP the data (the safe default).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\WebWatcher');
    if DirExists(DataDir) and (not UninstallSilent) then
    begin
      if MsgBox('Also delete your Web Watcher data?' + #13#10 + #13#10 +
                'This removes your watches, results, saved logins, and history at:' + #13#10 +
                DataDir + #13#10 + #13#10 +
                'Choose No to KEEP your data for a future reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
