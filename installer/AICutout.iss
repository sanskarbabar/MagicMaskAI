; AI Cutout — Windows installer (Inno Setup 6).
;
; Build:   scripts\build_installer.ps1     (produces dist\AICutout-Setup-<version>.exe)
; Installs: OFX plugin bundle  -> C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle
;           service+companion  -> C:\Program Files\AICutout
;           models             -> %PROGRAMDATA%\AICutout\models
;           per-user cache     -> %LOCALAPPDATA%\AICutout\cache   (created on first use, kept on uninstall)
; It never touches Resolve's own files; it only detects the Resolve installation to tell the user whether it was found.
; Defining DEV_USER_INSTALL builds a per-user variant (no admin, no Program Files) for testing the script itself.

#define AppName "AI Cutout"
#define AppVersion "0.1.0"
#define AppPublisher "AI Cutout"
#ifndef SourceRoot
  #define SourceRoot ".."
#endif

[Setup]
AppId={{6C1F3B0E-7B65-4E2B-9C3D-A1C0A11CE001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
#ifdef DEV_USER_INSTALL
DefaultDirName={localappdata}\AICutoutDev
#else
DefaultDirName={autopf}\AICutout
#endif
DefaultGroupName=AI Cutout
DisableProgramGroupPage=yes
OutputDir={#SourceRoot}\dist
OutputBaseFilename=AICutout-Setup-{#AppVersion}{#ifdef DEV_USER_INSTALL}-user{#endif}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName}
LicenseFile={#SourceRoot}\THIRD_PARTY_LICENSES.md
#ifdef DEV_USER_INSTALL
PrivilegesRequired=lowest
#else
PrivilegesRequired=admin
#endif

[Types]
Name: "full"; Description: "Full installation (plugin, AI service, Companion, models)"
Name: "nomodels"; Description: "Without model files (add them later)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "plugin"; Description: "DaVinci Resolve OFX plugin"; Types: full nomodels custom; Flags: fixed
Name: "service"; Description: "Local AI service and Companion"; Types: full nomodels custom; Flags: fixed
Name: "models"; Description: "SAM 2 tiny + small segmentation models (Apache-2.0). High Quality adds base_plus later via --fetch-models"; Types: full custom

[Files]
; --- service + companion (frozen) ---
Source: "{#SourceRoot}\dist\AICutout\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Components: service
Source: "{#SourceRoot}\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion; Components: service
; --- OFX plugin bundle ---
#ifdef DEV_USER_INSTALL
Source: "{#SourceRoot}\build\plugin\AICutout.ofx.bundle\*"; DestDir: "{app}\OFX\AICutout.ofx.bundle"; Flags: recursesubdirs ignoreversion; Components: plugin
#else
Source: "{#SourceRoot}\build\plugin\AICutout.ofx.bundle\*"; DestDir: "{commoncf64}\OFX\Plugins\AICutout.ofx.bundle"; Flags: recursesubdirs ignoreversion; Components: plugin
#endif
; --- models: shipped next to the installer (downloaded at build time, not stored in git) ---
Source: "{#SourceRoot}\models\weights\*.onnx"; Excludes: "sam2_hiera_base_plus.*"; DestDir: "{commonappdata}\AICutout\models"; Flags: ignoreversion skipifsourcedoesntexist; Components: models

[Icons]
Name: "{group}\AI Cutout Companion"; Filename: "{app}\aicutout-companion.exe"
Name: "{group}\Uninstall AI Cutout"; Filename: "{uninstallexe}"

[INI]
; the plugin reads this to start the local service on demand (no Windows service, no admin at runtime)
Filename: "{commonappdata}\AICutout\install.ini"; Section: "AICutout"; Key: "service"; String: """{app}\aicutout-service.exe"""
Filename: "{commonappdata}\AICutout\install.ini"; Section: "AICutout"; Key: "version"; String: "{#AppVersion}"

[Dirs]
Name: "{commonappdata}\AICutout"; Permissions: users-modify
Name: "{commonappdata}\AICutout\models"; Permissions: users-modify

[Run]
Filename: "{app}\aicutout-companion.exe"; Description: "Open the AI Cutout Companion"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM aicutout-service.exe"; Flags: runhidden skipifdoesntexist; RunOnceId: "StopService"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM aicutout-companion.exe"; Flags: runhidden skipifdoesntexist; RunOnceId: "StopCompanion"

[UninstallDelete]
Type: files; Name: "{commonappdata}\AICutout\install.ini"
; models are left in place only if the user asks (see UninstallCleanModels); the per-user cache is never deleted here.

[Code]
var
  ResolveInfo: String;

function FindResolve(): String;
var
  Path: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM, 'SOFTWARE\Blackmagic Design\DaVinci Resolve', 'Installation Path', Path) then Result := Path;
  if (Result = '') and DirExists(ExpandConstant('{commonpf64}\Blackmagic Design\DaVinci Resolve')) then
    Result := ExpandConstant('{commonpf64}\Blackmagic Design\DaVinci Resolve');
end;

function InitializeSetup(): Boolean;
var
  R: String;
begin
  Result := True;
  R := FindResolve();
  if R = '' then
  begin
    ResolveInfo := 'DaVinci Resolve was not found. The plugin will be installed anyway and appears the next time Resolve starts.';
    if not WizardSilent then
      MsgBox('DaVinci Resolve was not found on this computer.' + #13#10 + #13#10 +
             'You can continue: the plugin is placed in the standard OpenFX folder and will appear when Resolve is installed.',
             mbInformation, MB_OK);
  end
  else
    ResolveInfo := 'DaVinci Resolve found: ' + R;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpReady then
    WizardForm.ReadyMemo.Lines.Add(ResolveInfo);
end;

function NeedRestart(): Boolean;
begin
  Result := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    if MsgBox('Also remove the downloaded model files and the analysis cache?' + #13#10 +
              '(Models: ' + ExpandConstant('{commonappdata}\AICutout\models') + #13#10 +
              ' Cache: ' + ExpandConstant('{localappdata}\AICutout') + ')', mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{commonappdata}\AICutout'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\AICutout'), True, True, True);
    end;
end;
