; Inno Setup script (https://jrsoftware.org/isinfo.php) - per-user install, no admin rights.
; Run build-all.ps1 first, then open this file in Inno Setup and press Compile.
; Per-user folder %AppData%\Autodesk\Revit\Addins\<year> works for every Revit version,
; which avoids the Revit 2027 move of the all-users folder from ProgramData to Program Files.

#define AppName "My Revit Add-in"
#define AppVersion "1.0.0"
#define Bin "..\src\MyRevitAddin\bin\Release"

[Setup]
AppId={{7D3B6C1E-2F4A-4C8B-9E5D-1A2B3C4D5E6F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=My Company
DefaultDirName={userappdata}\Autodesk\Revit\Addins
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=MyRevitAddin-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Components]
Name: "r2023"; Description: "Revit 2023"; Types: full
Name: "r2024"; Description: "Revit 2024"; Types: full
Name: "r2025"; Description: "Revit 2025"; Types: full
Name: "r2026"; Description: "Revit 2026"; Types: full
Name: "r2027"; Description: "Revit 2027"; Types: full

[Files]
Source: "{#Bin}\2023\MyRevitAddin.addin"; DestDir: "{app}\2023"; Components: r2023; Flags: ignoreversion
Source: "{#Bin}\2023\MyRevitAddin.dll";   DestDir: "{app}\2023\MyRevitAddin"; Components: r2023; Flags: ignoreversion
Source: "{#Bin}\2024\MyRevitAddin.addin"; DestDir: "{app}\2024"; Components: r2024; Flags: ignoreversion
Source: "{#Bin}\2024\MyRevitAddin.dll";   DestDir: "{app}\2024\MyRevitAddin"; Components: r2024; Flags: ignoreversion
Source: "{#Bin}\2025\MyRevitAddin.addin"; DestDir: "{app}\2025"; Components: r2025; Flags: ignoreversion
Source: "{#Bin}\2025\MyRevitAddin.dll";   DestDir: "{app}\2025\MyRevitAddin"; Components: r2025; Flags: ignoreversion
Source: "{#Bin}\2026\MyRevitAddin.addin"; DestDir: "{app}\2026"; Components: r2026; Flags: ignoreversion
Source: "{#Bin}\2026\MyRevitAddin.dll";   DestDir: "{app}\2026\MyRevitAddin"; Components: r2026; Flags: ignoreversion
Source: "{#Bin}\2027\MyRevitAddin.addin"; DestDir: "{app}\2027"; Components: r2027; Flags: ignoreversion
Source: "{#Bin}\2027\MyRevitAddin.dll";   DestDir: "{app}\2027\MyRevitAddin"; Components: r2027; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\2023\MyRevitAddin"
Type: filesandordirs; Name: "{app}\2024\MyRevitAddin"
Type: filesandordirs; Name: "{app}\2025\MyRevitAddin"
Type: filesandordirs; Name: "{app}\2026\MyRevitAddin"
Type: filesandordirs; Name: "{app}\2027\MyRevitAddin"
