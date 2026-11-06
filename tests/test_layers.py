"""Layer building, run inside QGIS (offscreen). Skipped when QGIS is not importable."""
import json
from pathlib import Path

import pytest

qgis_core = pytest.importorskip("qgis.core")

from swathcast_qgis import layers  # noqa: E402

FIXTURE = Path(__file__).parent / "data" / "paraibuna_result.json"


def test_footprint_layer_from_live_result(qgs_app):
    result = json.loads(FIXTURE.read_text())
    labels = {"planned": "Planejada", "cycle_projected": "Projetada por ciclo",
              "archive_derived": "Estimada pelo histórico"}
    from swathcast_qgis.client import group_passes

    passes = group_passes(result["overpasses"])
    pass_of = {rid: p["id"] for p in passes for rid in p["row_ids"]}
    layer, fids = layers.footprint_layer(result, "test", labels, {"sentinel-2": "Sentinel-2"},
                                         pass_of)
    assert layer.isValid()
    n = len(result["overpasses"])
    assert layer.featureCount() == n == len(fids)
    feats = {f["row_id"]: f for f in layer.getFeatures()}
    row = result["overpasses"][0]
    f = feats[row["id"]]
    assert f["mission"] == row["mission"]
    assert f["evidence"] in labels.values() and f["evidence_code"] in labels
    assert f["pass_id"] == pass_of[row["id"]]
    assert f["datetime_utc"].toUTC().toString("yyyy-MM-ddTHH:mm") == row["timestamp_utc"][:16]
    assert not f.geometry().isEmpty()
    assert layer.temporalProperties().isActive()
    cats = layer.renderer().categories()
    assert {c.value() for c in cats} == {r["mission"] for r in result["overpasses"]}


def test_aoi_layer(qgs_app):
    g = qgis_core.QgsGeometry.fromWkt("POLYGON((-46 -23.6,-45.4 -23.6,-45.4 -23.2,-46 -23.2,-46 -23.6))")
    assert layers.aoi_layer(g, "aoi").featureCount() == 1
    p = qgis_core.QgsGeometry.fromWkt("POINT(-45.7 -23.4)")
    assert layers.aoi_layer(p, "aoi").geometryType() is not None


def test_fill_color_is_rgba_not_qt_argb():
    assert layers.rgba("#fd8d3c", 46) == "253,141,60,46"


def test_sensor_not_repeated():
    assert layers.sensor_label("CBERS-4A WFI", "WFI") == "CBERS-4A WFI"
    assert layers.sensor_label("Sentinel-2C", "MSI") == "Sentinel-2C MSI"


def test_mission_label(qgs_app):
    from swathcast_qgis.dock import SwathcastDock

    label = SwathcastDock._mission_label
    assert label({"long_name": "CBERS-4 WFI", "sensors": [{"name": "WFI", "swath_km": 866.0}]}) \
        == "CBERS-4 WFI (866 km)"
    assert label({"long_name": "Copernicus Sentinel-3", "sensors": [
        {"name": "OLCI", "swath_km": 1314.0}, {"name": "SLSTR", "swath_km": 1420.0}]}) \
        == "Sentinel-3 — OLCI (1314 km), SLSTR (1420 km)"


def test_pass_coverage_uses_the_union(qgs_app):
    from swathcast_qgis.client import group_passes

    aoi = "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
    half = [[[0, 0], [0.5, 0], [0.5, 1], [0, 1], [0, 0]]]
    result = {
        "overpasses": [{"id": 0, "timestamp_utc": "2026-10-20T13:00:00+00:00",
                        "date": "2026-10-20", "satellite": "S", "source": "plan",
                        "datatake_id": "d1", "grid_cell": "a"},
                       {"id": 1, "timestamp_utc": "2026-10-20T13:00:10+00:00",
                        "date": "2026-10-20", "satellite": "S", "source": "plan",
                        "datatake_id": "d1", "grid_cell": "b"}],
        # the same left half twice: a sum would say 100 %, the union says 50 %
        "footprints": {"features": [{"id": 0, "geometry": {"type": "Polygon", "coordinates": half}},
                                    {"id": 1, "geometry": {"type": "Polygon", "coordinates": half}}]},
    }
    passes = group_passes(result["overpasses"])
    assert len(passes) == 1
    cov = layers.pass_coverages(aoi, result, passes)
    assert abs(cov[0] - 0.5) < 0.01
    assert layers.pass_coverages("POINT(0.5 0.5)", result, passes) == {}



def test_footprints_have_a_soft_selection_colour(qgs_app):
    """Selecting a pass must not paint it the project's opaque yellow (QGIS >= 3.34)."""
    result = json.loads(FIXTURE.read_text())
    layer, _ = layers.footprint_layer(result, "test", {}, {})
    if not hasattr(layer, "selectionProperties"):
        pytest.skip("QGIS < 3.34: the project selection colour applies")
    props = layer.selectionProperties()
    assert props.selectionRenderingMode() == qgis_core.Qgis.SelectionRenderingMode.CustomColor
    assert props.selectionColor().alpha() < 255                       # translucent
