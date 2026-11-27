#!/usr/bin/env bash
# Builds the add-in for every Revit version and packs dist/CircuitManagerPro-Setup-<version>.exe
# Needs: .NET SDK 8 and 10, NSIS 3 (makensis). Works on Linux and on Windows (Git Bash).
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's:.*<Version>\(.*\)</Version>.*:\1:p' CircuitManagerPro.csproj)
YEARS="2022 2023 2024 2025 2026 2027"

rm -rf out
mkdir -p out/stage dist
for y in $YEARS; do
  echo "== Revit $y"
  dotnet build -c Release -p:RevitVersion="$y" -nologo -v q
  mkdir -p "out/stage/$y"
  cp "bin/Release/$y/CircuitManagerPro.dll" "out/stage/$y/"
  if [ -f "bin/Release/$y/CircuitManagerPro.deps.json" ]; then
    cp "bin/Release/$y/CircuitManagerPro.deps.json" "out/stage/$y/"
  fi
done
cp CircuitManagerPro.addin out/stage/

makensis -V2 -DVERSION="$VERSION" -DSTAGE="../out/stage" \
  -DOUTFILE="../dist/CircuitManagerPro-Setup-$VERSION.exe" installer/CircuitManagerPro.nsi
echo "Installer: dist/CircuitManagerPro-Setup-$VERSION.exe"
