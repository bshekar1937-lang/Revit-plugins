; =============================================================================
; Cable Tray Tools - Windows installer (Inno Setup 6)
; -----------------------------------------------------------------------------
; Builds a normal "Setup.exe" so users never copy files by hand.
; The installer:
;   * builds the pyRevit extension folder structure on the user's PC
;       %APPDATA%\pyRevit\Extensions\CableTrayTools.extension\
;           CableTray.tab\Routing.panel\AutoRoute Pro.pushbutton\script.py
;     (pyRevit loads every *.extension in this folder automatically)
;   * takes the script straight from the repository root - nothing is
;     duplicated in the repo, script.py is created at install time
;   * needs no admin rights (per-user install)
;   * adds an entry in Windows "Apps & features" for clean uninstall
;
; Build locally:   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\CableTrayTools.iss
; Build w/version: ISCC.exe /DAppVersion=1.2.0 installer\CableTrayTools.iss
; Output:          installer\Output\CableTrayTools-Setup-<version>.exe
; =============================================================================

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName       "Cable Tray Tools for Revit"
#define ExtName       "CableTrayTools.extension"
#define ButtonDir     "CableTray.tab\Routing.panel\AutoRoute Pro.pushbutton"

[Setup]
AppId={{84f6bdf6-dbc5-4dae-8d84-a9dc633b54ff}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Cable Tray Tools
DefaultDirName={userappdata}\pyRevit\Extensions\{#ExtName}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=CableTrayTools-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
CloseApplications=no

[Files]
; Single source of truth: the script in the repo root becomes script.py
Source: "..\CableTray_AutoRoute_Pro.py"; DestDir: "{app}\{#ButtonDir}"; DestName: "script.py"; Flags: ignoreversion
; Optional button icon - put a 32x32 (or 96x96) icon.png next to this .iss file
#if FileExists(AddBackslash(SourcePath) + "icon.png")
Source: "icon.png"; DestDir: "{app}\{#ButtonDir}"; Flags: ignoreversion
#endif

[UninstallDelete]
; remove anything pyRevit generated inside the extension (caches etc.)
Type: filesandordirs; Name: "{app}"

[Code]
function PyRevitInstalled(): Boolean;
begin
  Result :=
    DirExists(ExpandConstant('{userappdata}\pyRevit-Master')) or
    DirExists(ExpandConstant('{commonpf64}\pyRevit-Master')) or
    DirExists(ExpandConstant('{commonpf32}\pyRevit-Master')) or
    DirExists(ExpandConstant('{userappdata}\pyRevit')) or
    FileExists(ExpandConstant('{commonappdata}\Autodesk\Revit\Addins\2024\pyRevit.addin')) or
    FileExists(ExpandConstant('{commonappdata}\Autodesk\Revit\Addins\2025\pyRevit.addin')) or
    FileExists(ExpandConstant('{commonappdata}\Autodesk\Revit\Addins\2026\pyRevit.addin'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not PyRevitInstalled() then
    Result := MsgBox('pyRevit was not found on this computer.' + #13#10 + #13#10 +
      'This tool runs inside pyRevit. Install pyRevit first ' +
      '(https://github.com/pyrevitlabs/pyRevit/releases), or click Yes to ' +
      'continue anyway.', mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssDone) and not WizardSilent() then
    MsgBox('Installed.' + #13#10 + #13#10 +
      'If Revit is open, click pyRevit > Reload (or restart Revit).' + #13#10 +
      'The tool appears on the "CableTray" tab > Routing > AutoRoute Pro.',
      mbInformation, MB_OK);
end;
