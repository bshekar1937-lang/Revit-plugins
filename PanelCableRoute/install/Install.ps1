<#
.SYNOPSIS
    Installs (or removes) the Panel Cable Route Pro add-in for Revit.

.DESCRIPTION
    Copies the manifest into Revit's add-ins folder and the built assembly
    into a PanelCableRoute subfolder beside it, which is where the manifest
    expects it.

    Per user (the default):  %APPDATA%\Autodesk\Revit\Addins\<version>
    Every user (-AllUsers):  %PROGRAMDATA%\Autodesk\Revit\Addins\<version>
                             (needs an elevated PowerShell)

    Revit reads add-ins when it starts, so close and reopen Revit afterwards.

.EXAMPLE
    .\Install.ps1 -RevitVersion 2025

.EXAMPLE
    .\Install.ps1 -RevitVersion 2024 -BuildFirst

.EXAMPLE
    .\Install.ps1 -RevitVersion 2025 -Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('2023', '2024', '2025', '2026', '2027')]
    [string] $RevitVersion,

    # Where the built PanelCableRoute.dll is. Defaults to the Release output.
    [string] $AssemblyPath,

    # Build the project before installing.
    [switch] $BuildFirst,

    # Install for every user instead of just this one.
    [switch] $AllUsers,

    # Remove the add-in instead of installing it.
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$manifest = Join-Path $root 'src\PanelCableRoute.addin'

$base = if ($AllUsers) { $env:ProgramData } else { $env:AppData }
$addinFolder = Join-Path $base "Autodesk\Revit\Addins\$RevitVersion"
$assemblyFolder = Join-Path $addinFolder 'PanelCableRoute'

if ($Uninstall) {
    $installedManifest = Join-Path $addinFolder 'PanelCableRoute.addin'
    if (Test-Path $installedManifest) {
        Remove-Item $installedManifest -Force
        Write-Host "Removed $installedManifest"
    }
    if (Test-Path $assemblyFolder) {
        Remove-Item $assemblyFolder -Recurse -Force
        Write-Host "Removed $assemblyFolder"
    }
    Write-Host 'Restart Revit to finish removing it.'
    return
}

if ($BuildFirst) {
    $project = Join-Path $root 'src\PanelCableRoute.csproj'
    Write-Host "Building for Revit $RevitVersion ..."
    # DeployAddin=false: this script does the copying, not the build.
    & dotnet build $project -c Release `
        -p:RevitVersion=$RevitVersion -p:DeployAddin=false
    if ($LASTEXITCODE -ne 0) {
        throw 'The build failed; nothing was installed.'
    }
}

if (-not $AssemblyPath) {
    $AssemblyPath = Join-Path $root 'src\bin\Release\PanelCableRoute.dll'
}
if (-not (Test-Path $AssemblyPath)) {
    throw "PanelCableRoute.dll was not found at $AssemblyPath. Build the " +
          'project first (or pass -BuildFirst or -AssemblyPath).'
}
if (-not (Test-Path $manifest)) {
    throw "The manifest was not found at $manifest."
}

New-Item -ItemType Directory -Path $assemblyFolder -Force | Out-Null
Copy-Item $manifest -Destination $addinFolder -Force
Copy-Item $AssemblyPath -Destination $assemblyFolder -Force

$pdb = [System.IO.Path]::ChangeExtension($AssemblyPath, '.pdb')
if (Test-Path $pdb) {
    Copy-Item $pdb -Destination $assemblyFolder -Force
}

Write-Host ''
Write-Host "Installed for Revit $RevitVersion"
Write-Host "  manifest: $(Join-Path $addinFolder 'PanelCableRoute.addin')"
Write-Host "  assembly: $(Join-Path $assemblyFolder 'PanelCableRoute.dll')"
Write-Host ''
Write-Host 'Restart Revit, open a plan view, then look for the'
Write-Host '"Electrical Tools" ribbon tab.'
