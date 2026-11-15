# Builds one Release DLL per Revit version into src\MyRevitAddin\bin\Release\<year>\
# Run from a "Developer PowerShell for VS 2022" (or any shell with the .NET SDKs on PATH).
$ErrorActionPreference = "Stop"
$versions = 2023, 2024, 2025, 2026, 2027
foreach ($v in $versions) {
    Write-Host "=== Revit $v ===" -ForegroundColor Cyan
    dotnet build "$PSScriptRoot\src\MyRevitAddin\MyRevitAddin.csproj" -c Release -p:RevitVersion=$v
}
Write-Host "Done. Now compile installer\MyRevitAddin.iss with Inno Setup." -ForegroundColor Green
