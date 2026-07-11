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
; Show the welcome page (Inno 6 hides it by default) so the first screen can say whether this
; is a fresh install or an update — see InitializeWizard in [Code].
DisableWelcomePage=no
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
; ONE clean finish-page checkbox: "Launch Web Watcher now". First-run provisioning (Ollama +
; model pulls) is NOT run here — the launcher does it: launcher.py's _needs_setup()/_run_setup()
; runs provision in a visible console BEFORE opening the app whenever setup hasn't completed
; (marker absent). Running provision.py as its own postinstall entry showed a confusing bare
; "run python …provision.py" checkbox (it has no Description) that a user could uncheck; dropping
; it leaves a single obvious action, and the model download just shifts to first launch.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Description: "Launch {#AppName} now"; Flags: postinstall nowait skipifsilent

; SILENT install = the app updated itself (updater.launch_installer runs us with /SILENT after
; closing its own window). There is no finish page to hold the checkbox above, so relaunch here
; instead — otherwise the app would vanish mid-update and never come back.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Flags: nowait; Check: WizardSilent

; NOTE: no [UninstallRun] to remove shortcuts — Inno automatically removes the [Icons] it
; created. We deliberately do NOT run uninstall.py here: it deletes "Web Watcher" shortcuts by
; NAME from the user's Desktop/Start Menu, which could clobber a *different* install's shortcuts.
; Data deletion is handled by the opt-in prompt in [Code] below.

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nWeb Watcher bundles its own Python, so nothing else is required up front. On first launch it will download the local AI models (several GB, one time) via Ollama — an internet connection is needed for that step, but the app runs fully offline afterward.

[Code]
// Uninstall registry subkey Inno writes for this AppId (per-user → HKCU, elevated → HKLM).
const
  UninstallSubkey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E1D9A4C-2F5B-4C8E-9A31-0B2C3D4E5F60}_is1';

// The version of any already-installed Web Watcher, or '' if this is a fresh machine.
function PriorVersion(): string;
var
  v: string;
begin
  Result := '';
  if RegQueryStringValue(HKCU, UninstallSubkey, 'DisplayVersion', v) then
    Result := v
  else if RegQueryStringValue(HKLM, UninstallSubkey, 'DisplayVersion', v) then
    Result := v;
end;

// Close any running Web Watcher before we touch files. Without this, an app left running
// during an install/uninstall (a) holds files the installer wants to replace/delete, and
// (b) re-writes its chat history + config right after an uninstall's data wipe — the
// "reset to fresh install kept my chat history" bug. The window title is exactly
// "Web Watcher", so a title-filtered taskkill hits only us (not other python apps).
procedure KillRunningApp();
var
  R: Integer;
begin
  // Graceful first (WM_CLOSE → the app saves state and stops services), force what's left.
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/FI "WINDOWTITLE eq Web Watcher*"', '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(2500);
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /FI "WINDOWTITLE eq Web Watcher*"', '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(500);  // let file handles release before we delete/replace
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunningApp();
  Result := '';
end;

// If Web Watcher is already installed, reframe the first screen as an UPDATE (not a fresh
// install) and reassure the user their data is kept. The same installer handles both — Inno
// upgrades in place because the AppId matches — so this is messaging, not a code path change.
procedure InitializeWizard();
var
  prev: string;
begin
  prev := PriorVersion();
  if prev <> '' then
  begin
    WizardForm.WelcomeLabel1.Caption := 'Update Web Watcher';
    WizardForm.WelcomeLabel2.Caption :=
      'Web Watcher ' + prev + ' is already installed on this computer.' + #13#10 + #13#10 +
      'Setup will update it to version {#AppVersion}. Your watches, saved logins, results, ' +
      'and history are all kept — nothing personal is removed.' + #13#10 + #13#10 +
      'Click Next to update.';
  end;
end;

// On uninstall, offer to also delete the user's data. It lives OUTSIDE the app folder
// (%LOCALAPPDATA%\WebWatcher — watches, results, saved logins, logs, DB), so a normal
// uninstall keeps it for a future reinstall. Give the user a clear one-time choice.
// In a silent uninstall we never prompt and always KEEP the data (the safe default).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usUninstall then
    KillRunningApp();   // a still-running app would resurrect chat history/config post-wipe
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
