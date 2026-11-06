#!/usr/bin/env bash
# Run the test suite inside a conda env that has QGIS (default: qgis-dev).
#   scripts/test.sh            # QGIS 4 (Qt6)
#   scripts/test.sh qgis-ltr   # QGIS 3.40 LTR (Qt5)
set -euo pipefail
ENV="${1:-qgis-dev}"
PREFIX="$(conda info --base)/envs/$ENV"
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen
export PYTHONPATH="$PREFIX/share/qgis/python${PYTHONPATH:+:$PYTHONPATH}"
export PROJ_DATA="$PREFIX/share/proj" GDAL_DATA="$PREFIX/share/gdal"
exec "$PREFIX/bin/python" -m pytest -q -p no:cacheprovider tests "${@:2}"
