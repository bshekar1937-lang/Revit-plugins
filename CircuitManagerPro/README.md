# Circuit Manager Pro – Revit add-in

A standalone Revit add-in (no pyRevit or Dynamo needed) for Revit 2022–2027. It adds a
**Circuit Manager** tab with one button, **Circuit Manager Pro**. The button opens a panel
that stays open while you keep working in Revit. The panel has four tabs:

1. **Create circuits**: pick fixtures and give each one its own power circuit,
   optionally on a panel you choose.
2. **Move / remove circuits**: move, disconnect or delete many circuits at once.
3. **Add / remove elements**: change the elements on a circuit.
4. **Load classification check**: see which load classification each load really uses,
   find duplicate or nearly identical classifications, set loads to one classification,
   and create, rename, clean, delete or merge classifications.

Every action is one Undo step.

## Install (for users)

Run `CircuitManagerPro-Setup-<version>.exe`, keep the Revit versions it ticked, click
**Install**, then start Revit. Administrator rights aren't needed. The first time Revit
starts, it asks about an add-in without a digital signature: choose **Always Load**.
To uninstall, go to Windows **Settings > Apps > Installed apps > Circuit Manager Pro**.

Files are installed per user to:

```
%APPDATA%\Autodesk\Revit\Addins\<year>\CircuitManagerPro.addin
%APPDATA%\Autodesk\Revit\Addins\<year>\CircuitManagerPro\CircuitManagerPro.dll
```

## Build (for developers)

You need the .NET SDK 8 and 10, plus NSIS 3 (`makensis`). The Revit API reference
assemblies are downloaded from NuGet (`Nice3point.Revit.Api.*`), so you don't need Revit
installed to build.

```bash
./build.sh          # builds all Revit versions and creates dist/CircuitManagerPro-Setup-<version>.exe
```

To build a single Revit version:

```bash
dotnet build -c Release -p:RevitVersion=2025    # output in bin/Release/2025/
```

| Revit | .NET | Notes |
|---|---|---|
| 2022–2024 | .NET Framework 4.8 | `ElementId.IntegerValue` before 2024 |
| 2025–2026 | .NET 8 | |
| 2027 | .NET 10 | |

To change the version number, edit `<Version>` in `CircuitManagerPro.csproj`.

## Layout

```
src/App.cs          ribbon button, ExternalEvent, ribbon command
src/ApiQueue.cs     runs panel actions inside Revit's API context (modeless panel)
src/MainWindow.cs   the panel: all four tabs
src/LoadClass.cs    load classification check and editing
src/Rvt.cs          Revit helpers (ids across versions, transactions, labels)
Resources/MainWindow.xaml   the panel's layout and 3D styling
installer/          NSIS installer script and icon
```
