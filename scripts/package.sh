#!/usr/bin/env bash
# Build the installable ZIP (QGIS: Plugins > Manage and Install Plugins > Install from ZIP).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^version=//p' swathcast_qgis/metadata.txt)
mkdir -p dist
OUT="dist/swathcast_qgis-$VERSION.zip"
python3 - "$OUT" <<'PY'
import sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(Path("swathcast_qgis").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".ts"):
            z.write(p)
    z.write("LICENSE", "swathcast_qgis/LICENSE")  # plugins.qgis.org wants it in the plugin folder
PY
echo "$OUT"
