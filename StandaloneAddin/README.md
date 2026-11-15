# My Revit Add-in: standalone C# starter

A standalone Revit add-in is a compiled C# DLL plus a small `.addin` manifest file.
Revit loads it at startup with nothing else installed: no pyRevit and no Dynamo.
This folder is a working starter project. It puts a **My Tools** tab on the ribbon with two buttons:

| Button | What it shows you |
|---|---|
| **Hello Revit** | A read-only command: reads the selection and shows a dialog |
| **Count Walls** | A command that changes the model: it writes the Comments parameter inside one Transaction, so one Ctrl+Z undoes it |

---

## 1. Requirements

### Software (Windows PC)

| Item | Version | Notes |
|---|---|---|
| Windows 10/11 x64 | — | Revit add-ins run on Windows only |
| Autodesk Revit | 2023 – 2027 | Needed to run and debug the add-in. You can compile without it (see the NuGet note below) |
| Visual Studio 2022 Community (free) | **17.8 or later**; use the latest 17.x for .NET 10 | Workload: **.NET desktop development** |
| .NET Framework 4.8 Developer Pack | 4.8 | For Revit **2023 – 2024** |
| .NET 8 SDK | 8.0.100+ | For Revit **2025 – 2026** |
| .NET 10 SDK | 10.0.100+ | For Revit **2027** |
| Inno Setup (free) | 6.x | Only needed to build an installer `.exe` |
| RevitLookup (free, recommended) | Same year as your Revit | Lets you look inside elements and parameters while you develop: github.com/jeremytammik/RevitLookup |

### Which .NET goes with which Revit

| Revit | Target framework | Build command |
|---|---|---|
| 2023, 2024 | `net48` | `dotnet build -c Release -p:RevitVersion=2024` |
| 2025, 2026 | `net8.0-windows` | `dotnet build -c Release -p:RevitVersion=2025` |
| 2027 | `net10.0-windows` | `dotnet build -c Release -p:RevitVersion=2027` |

A DLL built for one .NET generation will **not** load in Revit versions that use a different one.
That is why the project builds one DLL per Revit year. The csproj picks the right framework for you.

### Revit API references

The project uses the `Nice3point.Revit.Api.RevitAPI` / `RevitAPIUI` NuGet packages. They contain
reference copies of `RevitAPI.dll` and `RevitAPIUI.dll` for each Revit year, so you can build every
version on one PC. `ExcludeAssets="runtime"` stops those DLLs being copied into your output, because
Revit supplies its own. If you prefer to use the files from your Revit install instead, reference
`C:\Program Files\Autodesk\Revit 20XX\RevitAPI.dll` and `RevitAPIUI.dll` and set **Copy Local = False**.

### Skills to learn, in order

1. **C# basics**: classes, `List<T>`, LINQ, `using` blocks, try/catch
2. **Revit API core**: `IExternalApplication` (startup and ribbon), `IExternalCommand` (one button),
   `Document`, `UIDocument`, `FilteredElementCollector`, `Transaction`, `Parameter`, `ElementId`
3. **Units**: Revit stores lengths internally in decimal **feet**. Convert with `UnitUtils.ConvertToInternalUnits(value, UnitTypeId.Millimeters)`
4. **UI**: `TaskDialog` for messages; WPF windows for forms
5. **Selection**: `uiDoc.Selection.PickObjects(...)` with an `ISelectionFilter`
6. **Deployment**: the `.addin` manifest, the add-in folders, and an installer

---

## 2. Project layout

```
StandaloneAddin/
├─ MyRevitAddin.sln
├─ build-all.ps1                 builds 2023-2027 Release DLLs
├─ installer/MyRevitAddin.iss    Inno Setup script (per-user install, no admin)
└─ src/MyRevitAddin/
   ├─ MyRevitAddin.csproj        picks .NET by RevitVersion; auto-deploys Debug builds
   ├─ MyRevitAddin.addin         manifest Revit reads at startup
   ├─ App.cs                     IExternalApplication: creates the ribbon tab and buttons
   ├─ Commands/HelloCommand.cs   read-only example
   ├─ Commands/CountWallsCommand.cs   Transaction example
   └─ Resources/icon16.png, icon32.png
```

---

## 3. Step by step

1. **Install** Visual Studio 2022, the three .NET SDKs/packs above, and Revit.
2. **Open** `MyRevitAddin.sln`.
3. **Make it yours.** In `MyRevitAddin.addin`, replace `AddInId` with a new GUID
   (Tools → Create GUID). Also change `VendorId` and the name. Use the same GUID as `AppId` in the `.iss` file.
4. **Choose your Revit year.** Edit `<RevitVersion>` in the csproj (default 2025), or
   pass `-p:RevitVersion=2026` on the command line.
5. **Build in Debug.** The `DeployToRevit` target copies the files to
   `%AppData%\Autodesk\Revit\Addins\<year>\`:
   ```
   MyRevitAddin.addin
   MyRevitAddin\MyRevitAddin.dll
   ```
   Close Revit before you build, because Revit locks the DLL while it is running.
6. **Debug.** Project → Properties → Debug → *Start external program*:
   `C:\Program Files\Autodesk\Revit 2025\Revit.exe`. Press F5, set breakpoints, then click
   your button. The first time it loads, Revit shows a **"Load Always / Load Once"** security prompt for an unsigned add-in.
7. **Add your own tool.** Copy `HelloCommand.cs`, rename the class, and add a
   `PushButtonData` for it in `App.cs`.
8. **Release.** Run `build-all.ps1`, then compile `installer\MyRevitAddin.iss` in Inno Setup to get
   `MyRevitAddin-1.0.0-Setup.exe`.

### Add-in folders Revit scans

| Scope | Path |
|---|---|
| Current user (all years) | `%AppData%\Autodesk\Revit\Addins\20XX\` |
| All users, Revit ≤ 2026 | `C:\ProgramData\Autodesk\Revit\Addins\20XX\` |
| All users, **Revit 2027+** | `C:\Program Files\Autodesk\Revit\Addins\20XX\` (moved in 2027; needs admin) |

The installer uses the per-user folder. It works for every year and needs no admin rights.

### Optional but recommended for distribution

- **Code-sign the DLL** with a code-signing certificate (`signtool`). Users of a signed add-in
  get no "unsigned add-in" warning.
- **Autodesk App Store** packaging uses a `.bundle` folder with `PackageContents.xml`, installed to
  `%AppData%\Autodesk\ApplicationPlugins`.
- **Add-in isolation / dependency conflicts.** If you ship third-party DLLs (Newtonsoft, etc.),
  another add-in may load a different version first. Revit 2026+ has add-in dependency isolation
  for .NET 8+; check the "What's New" page of the Revit API for your year.

---

## 4. Version gotchas

| Topic | Change |
|---|---|
| `ElementId.IntegerValue` | Replaced by `ElementId.Value` (a `long`) from 2024; **removed in 2026** |
| Units | `DisplayUnitType` was removed in 2022. Use `UnitTypeId` / `SpecTypeId` |
| .NET 8 (2025) | Recompile; some APIs differ (`System.Drawing`, `BinaryFormatter`, WinForms designer) |
| .NET 10 (2027) | Install SDK 10.0.100; all-users add-in folder moved; `CloudRegionUS/EMEA` replaced by `ModelPathUtils.GetAllCloudRegions()` |

Use `#if REVIT2024` etc. for version-specific code. The csproj defines `REVIT<year>`.

---

## 5. YouTube videos to learn from

These were found by search in Nov 2026. Some videos are several years old, so the Visual Studio
setup steps may look different. The API concepts have not changed.

**Getting started / first add-in**
- [Revit API Tutorial for Beginners – EP1: Installations and Requirements](https://www.youtube.com/watch?v=xB51voQJqVw)
- [Revit API C# Beginners Guide to Getting Started](https://www.youtube.com/watch?v=C0mNU2bEUSs)
- [Revit Pure Live #008 – Creating Your First Add-in Using C# (Michael Kilkelly)](https://www.youtube.com/watch?v=mHfnsNOEbms)
- [C# + Revit: Lesson 1 – Getting started!](https://www.youtube.com/watch?v=PPX02PVhLbo)
- [From Zero, Step by Step C# for Revit API Plug-in Creation](https://www.youtube.com/watch?v=WqOy65EV0ls)

**Full courses / playlists**
- [Revit API Course For Beginners (playlist)](https://www.youtube.com/playlist?list=PLJHSZmHJLILEvGnmUPfI0R4oCplcDq8sY)
- [Revit API – Plugin Development (playlist)](https://www.youtube.com/playlist?list=PL7mg6Q0pUAfn8Pa7cyfRxTERRML964bUE)

**Ribbon tab and buttons**
- [Revit API C# – Add a Tab and Button](https://www.youtube.com/watch?v=429I8jfjlyk)
- [Revit API 04 – Ribbon Button](https://www.youtube.com/watch?v=lYjHDkpbsas)

**Installing / packaging**
- [Installing Revit Add-ins](https://www.youtube.com/watch?v=17v0sN663g0)
- [Create a software installer with Inno Setup](https://www.youtube.com/watch?v=OSIGcVI13Lw)
- [Detailed Inno Setup Tutorial](https://www.youtube.com/watch?v=4s4rP9GYH0o)

**Search terms that work well on YouTube:** "Revit API WPF window", "Revit API ISelectionFilter",
"Revit API FilteredElementCollector", "Revit API ExternalEvent modeless".

## 6. Written references

- Revit 2027 SDK, .NET 10 and API changes: https://blog.autodesk.io/revit-2027-sdk-net-10-api-changes-and-additions/
- Migrating Revit to .NET 10 (Autodesk help): https://help.autodesk.com/cloudhelp/2027/ENU/Revit-WhatsNew/files/GUID-8D7A4715-EAF8-4BD1-BE78-061F900D0BCE.htm
- Migrating from .NET 4.8 to .NET 8 (The Building Coder): https://jeremytammik.github.io/tbc/a/doc/migrating_to_net_core_8.html
- Searchable API docs for every year: https://rvtdocs.com
- Revit API forum: https://forums.autodesk.com/t5/revit-api-forum/bd-p/160
- Add-in Registration (manifest reference): https://help.autodesk.com/cloudhelp/2024/ENU/Revit-API/files/Revit_API_Developers_Guide/Introduction/Add_In_Integration/Revit_API_Revit_API_Developers_Guide_Introduction_Add_In_Integration_Add_in_Registration_html.html
