; =============================================================================
; Circuit Manager Pro - Windows installer (NSIS 3)
; -----------------------------------------------------------------------------
; Per-user install, no administrator rights needed. For every Revit version
; chosen on the Components page it copies
;     %APPDATA%\Autodesk\Revit\Addins\<year>\CircuitManagerPro.addin
;     %APPDATA%\Autodesk\Revit\Addins\<year>\CircuitManagerPro\CircuitManagerPro.dll
; Revit versions found in the default install folder are ticked automatically.
; An uninstaller is added to Windows "Installed apps".
;
; Build (see ../build.sh):
;   makensis -DVERSION=1.0.0 -DSTAGE=<staged files> -DOUTFILE=<setup.exe> CircuitManagerPro.nsi
; =============================================================================
Unicode True
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef STAGE
  !define STAGE "..\out\stage"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\CircuitManagerPro-Setup-${VERSION}.exe"
!endif

!define APPNAME    "Circuit Manager Pro"
!define PUBLISHER  "Circuit Manager Pro"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\CircuitManagerPro"
!define ADDINS     "$APPDATA\Autodesk\Revit\Addins"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$APPDATA\CircuitManagerPro"
RequestExecutionLevel user
ShowInstDetails show
ShowUninstDetails show
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} setup - Revit add-in"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PUBLISHER}"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"

!define MUI_ICON   "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_SMALLDESC

!define MUI_WELCOMEPAGE_TITLE "Install ${APPNAME} ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "This installs the ${APPNAME} add-in for Autodesk Revit.$\r$\n$\r$\n\
  - Create one circuit per fixture$\r$\n\
  - Move, disconnect, delete circuits in bulk$\r$\n\
  - Add or remove elements on a circuit$\r$\n\
  - Check and fix load classifications$\r$\n$\r$\n\
  Please close Revit before you continue."
!insertmacro MUI_PAGE_WELCOME

!define MUI_PAGE_HEADER_TEXT "Choose Revit versions"
!define MUI_PAGE_HEADER_SUBTEXT "Revit versions found on this PC are ticked already."
!define MUI_COMPONENTSPAGE_TEXT_TOP "Tick every Revit version that should get the add-in. Versions installed in the default folder are ticked automatically."
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "${APPNAME} is installed"
!define MUI_FINISHPAGE_TEXT "Start Revit, open a project and click$\r$\n\
  Circuit Manager tab  >  Circuit Manager Pro.$\r$\n$\r$\n\
  The first time, Revit asks about an add-in without a digital signature - choose $\"Always Load$\".$\r$\n$\r$\n\
  To remove it later use Windows Settings > Apps > Installed apps > ${APPNAME}."
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; -----------------------------------------------------------------------------
; Revit must be closed: a loaded add-in DLL cannot be replaced
; -----------------------------------------------------------------------------
!macro CheckRevitClosed UN
Function ${UN}CheckRevitClosed
  check:
    ; exit code 0 = "find" saw Revit.exe in the process list
    nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq Revit.exe" /NH | find /I "Revit.exe"'
    Pop $0
    Pop $1
    ${If} $0 == 0
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Revit is running.$\r$\n$\r$\nSave your work, close Revit, then click Retry." IDRETRY check
      Abort
    ${EndIf}
FunctionEnd
!macroend
!insertmacro CheckRevitClosed ""
!insertmacro CheckRevitClosed "un."

; -----------------------------------------------------------------------------
; One section per Revit version
; -----------------------------------------------------------------------------
Section "-Core"
  SetOutPath "$INSTDIR"
  File "icon.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayName"     "${APPNAME} (Revit add-in)"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKCU "${UNINST_KEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\icon.ico"
  WriteRegStr   HKCU "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr   HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" 600
SectionEnd

!macro RevitSection YEAR
Section "Revit ${YEAR}" SEC_${YEAR}
  SetOutPath "${ADDINS}\${YEAR}\CircuitManagerPro"
  File /r "${STAGE}\${YEAR}\*.*"
  SetOutPath "${ADDINS}\${YEAR}"
  File "${STAGE}\CircuitManagerPro.addin"
  DetailPrint "Installed for Revit ${YEAR}"
SectionEnd
!macroend

!insertmacro RevitSection 2022
!insertmacro RevitSection 2023
!insertmacro RevitSection 2024
!insertmacro RevitSection 2025
!insertmacro RevitSection 2026
!insertmacro RevitSection 2027

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2022} "Add-in for Autodesk Revit 2022"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2023} "Add-in for Autodesk Revit 2023"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2024} "Add-in for Autodesk Revit 2024"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2025} "Add-in for Autodesk Revit 2025"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2026} "Add-in for Autodesk Revit 2026"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_2027} "Add-in for Autodesk Revit 2027"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; -----------------------------------------------------------------------------
; Tick the Revit versions that are installed
; -----------------------------------------------------------------------------
!macro Detect YEAR
  ${If} ${FileExists} "$PROGRAMFILES64\Autodesk\Revit ${YEAR}\Revit.exe"
    !insertmacro SelectSection ${SEC_${YEAR}}
    IntOp $R0 $R0 + 1
  ${Else}
    !insertmacro UnselectSection ${SEC_${YEAR}}
  ${EndIf}
!macroend

Function .onInit
  Call CheckRevitClosed
  StrCpy $R0 0
  !insertmacro Detect 2022
  !insertmacro Detect 2023
  !insertmacro Detect 2024
  !insertmacro Detect 2025
  !insertmacro Detect 2026
  !insertmacro Detect 2027
  ${If} $R0 == 0
    MessageBox MB_OK|MB_ICONINFORMATION "No Revit 2022 - 2027 was found in the default folder.$\r$\n$\r$\nOn the next pages, tick the Revit versions you use."
  ${EndIf}
FunctionEnd

; -----------------------------------------------------------------------------
; Uninstall: remove the add-in from every Revit version
; -----------------------------------------------------------------------------
!macro Remove YEAR
  Delete "${ADDINS}\${YEAR}\CircuitManagerPro.addin"
  RMDir /r "${ADDINS}\${YEAR}\CircuitManagerPro"
!macroend

Function un.onInit
  Call un.CheckRevitClosed
FunctionEnd

Section "Uninstall"
  !insertmacro Remove 2022
  !insertmacro Remove 2023
  !insertmacro Remove 2024
  !insertmacro Remove 2025
  !insertmacro Remove 2026
  !insertmacro Remove 2027
  Delete "$INSTDIR\icon.ico"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
