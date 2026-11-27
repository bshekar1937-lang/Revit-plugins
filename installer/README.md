# Installer

Builds `CableTrayTools-Setup-<version>.exe`, a normal Windows setup wizard.
Users run it and the tool shows up in Revit, with no files to copy by hand.

## What the installer does
1. Checks that pyRevit is installed and warns if it isn't.
2. Creates the pyRevit extension on the user's PC:
   ```
   %APPDATA%\pyRevit\Extensions\CableTrayTools.extension\
       CableTray.tab\Routing.panel\AutoRoute Pro.pushbutton\script.py
   ```
   pyRevit loads every `*.extension` folder in this location by itself.
3. Adds an uninstall entry under Windows *Settings > Apps*.

It needs no admin rights. `script.py` is made at install time from
`CableTray_AutoRoute_Pro.py` in the repo root, so the repo holds only one copy
of the script.

## Build it

**Option A: GitHub (no software needed on your PC)**
- *Actions* tab > **Build installer** > *Run workflow*, then download the
  `CableTrayTools-Setup` artifact, **or**
- push a tag such as `v1.0.0`. The `.exe` is built and attached to a GitHub
  Release automatically.

**Option B: on a Windows PC**
1. Install [Inno Setup 6](https://jrsoftware.org/isdl.php) (free).
2. Run:
   ```
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 installer\CableTrayTools.iss
   ```
   Or open the `.iss` file in Inno Setup and press **Compile**.
3. The installer is written to `installer\Output\`.

## Optional
- Put an `icon.png` (32×32 or 96×96) in this folder to give the ribbon
  button an icon. The build picks it up automatically.
- To add more tools later, add another `[Files]` line in the `.iss` file that
  points to a new `...pushbutton` folder.
- Silent install, for IT deployment: `CableTrayTools-Setup-1.0.0.exe /VERYSILENT`
