#!/usr/bin/env bash
# Render the README figures (see scripts/screenshots.py) in a conda env with QGIS.
#   scripts/screenshots.sh           # English
#   scripts/screenshots.sh pt_BR     # Portuguese
set -euo pipefail
ENV="${ENV:-qgis-dev}"
PREFIX="$(conda info --base)/envs/$ENV"
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen QGIS_PREFIX_PATH="$PREFIX"
export PYTHONPATH="$PREFIX/share/qgis/python${PYTHONPATH:+:$PYTHONPATH}"
export PROJ_DATA="$PREFIX/share/proj" GDAL_DATA="$PREFIX/share/gdal"
exec "$PREFIX/bin/python" scripts/screenshots.py "$@"
