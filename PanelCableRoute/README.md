# Panel Cable Route Pro — standalone Revit add-in

Panel-to-panel cable tray route lengths for voltage-drop calculations, as a
native Revit add-in. **No pyRevit, no Dynamo, no IronPython** — it is a C#
assembly that Revit loads itself, with its own ribbon tab.

It is the same tool as `PanelCableRoute_Pro.py` in the repository root, rebuilt
as a compiled add-in.

---

## What it does

1. **Setup dialog** — what to measure (every panel pair, the equipment on each
   panel's circuits, isolators), which panels, the cable allowance, mV/A/m, the
   voltage-drop limit, fallback current and voltage, and the tray connection
   tolerance.
2. **Measures** the route along the modelled cable tray centre lines. One
   shortest-path pass per panel serves all of that panel's destinations, so a
   building of *P* panels costs *P* passes rather than one per pair.
3. **Preview panel**, opening on a 3D view:
   - **3D preview** — the tray network, the panels and every route, orbit with
     the mouse. Routes over the limit are red; the route selected in the table
     is highlighted.
   - **Routes** — a sortable, filterable table: lengths, current, voltage,
     drop in volts and per cent, status. Tick the routes to draw and export;
     select or zoom to either end in Revit.
   - **Panels** — per panel: how many routes touch it, its longest route and
     its worst drop.
   - **Overview and issues** — the report, equipment that could not be reached
     and routes that had to bridge a gap in the trays.
4. **Draws** the ticked routes as red detail lines in the plan views of both
   end levels, replacing a previous run's lines, in one transaction.
5. **Exports** to CSV, or straight into Excel with the header frozen and
   filters on, saved as `.xlsx` where Excel allows it.

Voltage drop is `mV/A/m × current × length ÷ 1000`, worked out on the length
**including** the cable allowance. The current and voltage come from the Revit
circuit when the connection is a circuit, otherwise from the dialog.

---

## Prerequisites

| | |
|---|---|
| Revit | 2023, 2024, 2025, 2026 or 2027 |
| Build tools | Visual Studio 2022 (17.8+) or the .NET SDK 8 |
| Target framework | Revit 2023–2024 → .NET Framework 4.8 · Revit 2025+ → .NET 8 |
| OS | **Windows only to build** — it references `RevitAPI.dll` and uses WPF |

## Building

From the `PanelCableRoute` folder:

```powershell
dotnet build src\PanelCableRoute.csproj -c Release -p:RevitVersion=2025
```

`RevitVersion` picks the target framework, the `RevitAPI.dll` to reference and
the add-ins folder to deploy to. If Revit is not in the usual place, add
`-p:RevitApiDir="D:\Autodesk\Revit 2025"`.

A successful build copies itself to
`%APPDATA%\Autodesk\Revit\Addins\<version>\` — pass `-p:DeployAddin=false` to
stop that.

## Installing

The build deploys itself; `install\Install.ps1` does the same job on a machine
with no build tools:

```powershell
.\install\Install.ps1 -RevitVersion 2025                 # install
.\install\Install.ps1 -RevitVersion 2024 -BuildFirst     # build, then install
.\install\Install.ps1 -RevitVersion 2025 -AllUsers       # every user (elevated)
.\install\Install.ps1 -RevitVersion 2025 -Uninstall      # remove
```

Files land as:

```
%APPDATA%\Autodesk\Revit\Addins\2025\
    PanelCableRoute.addin                    manifest Revit reads at startup
    PanelCableRoute\PanelCableRoute.dll      the add-in
```

**Restart Revit**, open a plan view, then use **Electrical Tools → Panel cable
routes**.

## Running it

Open the plan view you want the routes drawn in first: detail lines are
view-specific, and the tool refuses to start in a 3D or section view.

---

## Layout

```
src/
  PanelCableRoute.csproj      one project, version chosen by a property
  PanelCableRoute.addin       the manifest
  App.cs                      IExternalApplication: ribbon tab and button
  Commands/
    PanelRouteCommand.cs      IExternalCommand: reads, measures, previews, draws
  Core/                       no Revit references at all - see below
    Geometry.cs               Pt, units, orthogonal runs, box and tube corners
    Model.cs                  trays, equipment, circuits, options
    TrayNetwork.cs            the graph, the spatial index, attaching, Dijkstra
    Routing.cs                measuring one route, the record, voltage drop
    RouteBuilder.cs           the scope loop over panels, circuits, pairs
    Export.cs                 the table, CSV and the text report
  Revit/
    ModelReader.cs            collectors and parameters into the Core types
    RouteDrawing.cs           the red line style, views and the detail lines
  UI/
    SetupWindow.xaml(.cs)     the setup dialog
    ResultWindow.xaml(.cs)    the preview panel
    Scene3D.cs                builds the WPF 3D scene from the routes
    ExcelExport.cs            CSV file, and Excel through late-bound COM
    ProgressWindow.cs         progress and cancel while measuring
tests/
  CoreTests/                  checks for Core - runs anywhere, no Revit
  RevitStub/                  a stand-in for the Revit API, build checks only
  RevitLayerCheck/            compiles Core + Revit against the stub
install/Install.ps1
```

### Why Core has no Revit references

`Core` is plain C#: it takes points and tray segments, and gives back routes.
That is what lets the routing maths be compiled and tested on any machine,
including this one. `Revit/` is the only place that touches the API, and `UI/`
only draws.

## Tests

```powershell
dotnet run --project tests\CoreTests\CoreTests.csproj
```

Twelve groups of checks over the routing core, with no Revit and no test
framework: an L-shaped run, a broken tray run and the tolerance that closes
it, a loop, a riser preferred for equipment a floor below, a fitting bridging
two tray ends, the voltage-drop sums and the circuit current overriding the
dialog, the export table and CSV escaping, the panel summary, the 3D geometry,
the builder end to end (circuits, isolators behind plant, pairs, scope, length
limit, cancelling), and the wiring walk including a loop in it. It exits
non-zero on failure, so it drops into CI as it is.

```powershell
dotnet build tests\RevitLayerCheck\RevitLayerCheck.csproj
dotnet build tests\RevitLayerCheck\LegacyIdCheck.csproj
```

These compile `Core` + `Revit` against `tests/RevitStub`, which is a hand-written
stand-in for the slice of the Revit API the add-in uses. They prove the C# is
valid and internally consistent on any machine, and they compile **both**
branches of `ModelReader.IdOf` (`ElementId.Value` for 2024+, `IntegerValue` for
2023). They do **not** prove the stub matches the real API — only a build
against the real `RevitAPI.dll` does that.

---

## Honest state of it

**Tested here:** the whole of `Core`, by the checks above, all passing.
`Core` + `Revit` compile against the API stub, in both `ElementId`
configurations, at `LangVersion 7.3` so nothing can slip in that .NET
Framework 4.8 would reject. Both XAML files are well-formed, and every
`x:Name`, event handler and style key they use lines up with the code-behind.

**Not tested here, because it cannot be:** this container has no Revit and no
Windows, so nothing has been compiled against the real `RevitAPI.dll` and no
window has ever been rendered. Specifically unverified:

- the Revit API signatures in `ModelReader` and `RouteDrawing` — the first
  build on your machine is the real check, and that is the point of keeping
  that layer thin;
- `ElectricalSystem.Current` and `.Voltage` unit conversion. They go through
  `UnitUtils.ConvertFromInternalUnits` with `UnitTypeId.Amperes` / `Volts` and
  fall back to the raw value, so if the amps column looks wrong, that is the
  place to look;
- the WPF windows, the 3D viewport and the mouse orbit;
- the Excel export. It uses late-bound COM by reflection, which needs built-in
  COM interop — on by default for Revit's own process, but if it fails the CSV
  is already written and the message says where.

**Not included:** the cable tray tool (`CableTray_AutoRoute_Pro.py`) has not
been ported. The structure is set up for it: another `IExternalCommand` in
`Commands/`, its geometry in `Core/`, its dialogs in `UI/`, and one more
`PushButtonData` in `App.cs`.

The pyRevit scripts in the repository root still work and are unchanged. They
remain the quicker thing to edit; this add-in is the thing to deploy.
