#!/usr/bin/env bash
# Refresh translations: extract tr() strings, fill them from scripts/translations_*.py,
# compile the .qm files. Needs pylupdate6/lrelease6 (qgis-dev env).
set -euo pipefail
ENV="${1:-qgis-dev}"
BIN="$(conda info --base)/envs/$ENV/bin"
cd "$(dirname "$0")/.."
SRC=(swathcast_qgis/dock.py swathcast_qgis/plugin.py swathcast_qgis/welcome.py swathcast_qgis/info.py)
PT=swathcast_qgis/i18n/swathcast_pt_BR.ts
EN=swathcast_qgis/i18n/swathcast_en.ts
"$BIN/pylupdate6" "${SRC[@]}" --no-obsolete --ts "$PT"
python3 scripts/fill_ts.py "$PT" translations_pt_BR PT_BR pt_BR
"$BIN/lrelease6" "$PT" -qm swathcast_qgis/i18n/swathcast_pt_BR.qm
"$BIN/pylupdate6" "${SRC[@]}" --no-obsolete --ts "$EN"
python3 scripts/fill_ts.py "$EN" translations_en EN en --partial
"$BIN/lrelease6" "$EN" -qm swathcast_qgis/i18n/swathcast_en.qm
