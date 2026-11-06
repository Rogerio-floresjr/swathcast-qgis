"""Turn a swathview result into QGIS layers: footprints styled by mission, time-enabled."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsProperty,
    QgsRendererCategory,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QDateTime
from qgis.PyQt.QtGui import QColor

from . import compat
from .theme import TOKENS  # the shared SwathCast design tokens
from .client import evidence, geometry_to_multipolygon_wkt, join_rows, tasking_required

# (name, kind, source key in the overpass row)
FIELDS = (
    ("mission", "str", "mission"),
    ("satellite", "str", "satellite"),
    ("sensor", "str", "sensor"),
    ("pass_id", "int", None),
    ("evidence", "str", None),
    ("evidence_code", "str", None),
    ("tasking_required", "bool", None),
    ("source", "str", "source"),
    ("datetime_utc", "datetime", None),
    ("date", "date", None),
    ("local_time", "str", None),
    ("coverage_pct", "float", None),
    ("cloud_cover", "float", "cloud_cover"),
    ("sun_zenith_deg", "float", "sun_zenith_deg"),
    ("view_zenith_deg", "float", "view_zenith_deg"),
    ("cross_track_km", "float", "cross_track_km"),
    ("day_night", "str", "day_night"),
    ("mode", "str", "mode"),
    ("orbit_relative", "int", "orbit_relative"),
    ("orbit_absolute", "int", "orbit_absolute"),
    ("grid_cell", "str", "grid_cell"),
    ("datatake_id", "str", "datatake_id"),
    ("product_id", "str", "product_id"),
    ("projected_from", "str", "projected_from"),
    ("confidence", "str", "confidence"),
    ("row_id", "int", "id"),
)

# Stable colour per mission; families share a hue range (INPE greens, Sentinel blues...).
MISSION_COLORS = TOKENS["data"]["mission"]
_FALLBACK = tuple(TOKENS["data"]["mission_fallback"])

# Outline pattern carries the kind of evidence; colour stays the mission's.
STROKE_STYLE = ("CASE WHEN \"tasking_required\" THEN 'dot' "
                "WHEN \"evidence_code\" = 'cycle_projected' THEN 'dash' ELSE 'solid' END")
# ~6 %: wide swaths must not hide the study area or narrow tiles
FILL_ALPHA = round(TOKENS["data"]["footprint_fill_alpha"] * 255)
_SELECTION = TOKENS["data"]["selection"]
WGS84 = "EPSG:4326"


def mission_color(key: str) -> str:
    return MISSION_COLORS.get(key) or _FALLBACK[sum(map(ord, key)) % len(_FALLBACK)]


def rgba(hex_color: str, alpha: int) -> str:
    """'#rrggbb' + alpha → 'r,g,b,a' (QGIS reads an 8-digit hex as #AARRGGBB)."""
    h = hex_color.lstrip("#")
    return ",".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4)) + f",{alpha}"


def sensor_label(satellite: str, sensor: str) -> str:
    """'CBERS-4A WFI' + 'WFI' → 'CBERS-4A WFI' (some names already carry the sensor)."""
    if sensor and sensor not in satellite.split():
        return f"{satellite} {sensor}".strip()
    return satellite


def _utc(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def to_qdatetime(ts: datetime) -> QDateTime:
    return QDateTime.fromMSecsSinceEpoch(int(ts.timestamp() * 1000)).toUTC()


def _attributes(row: dict, evidence_labels: dict, pass_id: Optional[int]) -> list:
    ts = _utc(row.get("timestamp_utc"))
    qdt = to_qdatetime(ts) if ts else None
    cov = row.get("coverage_fraction")
    code = evidence(row)
    derived = {
        "pass_id": pass_id,
        "evidence": evidence_labels.get(code, code),
        "evidence_code": code,
        "tasking_required": tasking_required(row),
        "datetime_utc": qdt,
        "date": QDate(ts.year, ts.month, ts.day) if ts else None,
        "local_time": qdt.toLocalTime().toString("yyyy-MM-dd HH:mm") if qdt else None,
        "coverage_pct": round(cov * 100, 1) if cov is not None else None,
    }
    out = []
    for name, kind, key in FIELDS:
        value = derived[name] if key is None else row.get(key)
        if value is not None and kind == "str" and not isinstance(value, str):
            value = str(value)
        out.append(value)
    return out


def footprint_layer(result: dict, name: str, evidence_labels: dict, mission_names: dict,
                    pass_of: Optional[dict] = None) -> tuple[QgsVectorLayer, dict[int, int]]:
    """Memory layer of footprints (one feature per record) plus {row id: feature id}.

    ``pass_of`` maps a record's row id to its pass id (``client.group_passes``).
    """
    pass_of = pass_of or {}
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", name, "memory")
    # a translucent neutral selection, not the project's opaque yellow
    selection = QColor(_SELECTION["fill"])
    selection.setAlphaF(_SELECTION["fill_alpha"])
    compat.soft_selection(layer, selection)
    provider = layer.dataProvider()
    provider.addAttributes([compat.make_field(n, k) for n, k, _ in FIELDS])
    layer.updateFields()
    offset = QDateTime.currentDateTime().offsetFromUtc() // 60
    sign = "+" if offset >= 0 else "−"
    layer.setFieldAlias(layer.fields().indexOf("local_time"),
                        f"local_time (UTC{sign}{abs(offset) // 60:02d}:{abs(offset) % 60:02d})")

    feats = []
    for row, geom in join_rows(result):
        f = QgsFeature(layer.fields())
        f.setAttributes(_attributes(row, evidence_labels, pass_of.get(row.get("id"))))
        wkt = geometry_to_multipolygon_wkt(geom)
        if wkt:
            f.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(f)
    ok, added = provider.addFeatures(feats)
    if not ok:
        raise RuntimeError("could not add footprints to the memory layer")
    layer.updateExtents()
    row_idx = layer.fields().indexOf("row_id")
    fids = {f.attributes()[row_idx]: f.id() for f in added}

    style_by_mission(layer, mission_names)
    tp = layer.temporalProperties()
    tp.setMode(compat.INSTANT_FROM_FIELD)
    tp.setStartField("datetime_utc")
    tp.setIsActive(True)
    return layer, fids


def style_by_mission(layer: QgsVectorLayer, mission_names: dict) -> None:
    """One colour per mission; dashed = cycle-projected, dotted = tasking required.

    The legend counts passes per mission (records when no pass id is set).
    """
    idx = layer.fields().indexOf("mission")
    pidx = layer.fields().indexOf("pass_id")
    seen: dict = {}
    for f in layer.getFeatures():
        attrs = f.attributes()
        pid = attrs[pidx]
        seen.setdefault(attrs[idx], set()).add(pid if pid is not None else ("row", f.id()))
    counts = {k: len(v) for k, v in seen.items()}
    cats = []
    for key in sorted(counts, key=lambda k: mission_names.get(k, k or "")):
        color = mission_color(key or "")
        sym = QgsFillSymbol.createSimple({
            "color": rgba(color, FILL_ALPHA),
            "outline_color": color,
            "outline_width": "0.4",
        })
        sym.symbolLayer(0).setDataDefinedProperty(compat.STROKE_STYLE_PROPERTY,
                                                  QgsProperty.fromExpression(STROKE_STYLE))
        label = f"{mission_names.get(key, key)} ({counts[key]})"
        cats.append(QgsRendererCategory(key, sym, label))
    layer.setRenderer(QgsCategorizedSymbolRenderer("mission", cats))


def aoi_layer(geom_wgs84: QgsGeometry, name: str) -> QgsVectorLayer:
    kind = "Point" if geom_wgs84.asJson().find('"Point"') >= 0 else "MultiPolygon"
    layer = QgsVectorLayer(f"{kind}?crs=EPSG:4326", name, "memory")
    f = QgsFeature()
    f.setGeometry(geom_wgs84)
    layer.dataProvider().addFeatures([f])
    layer.updateExtents()
    if kind == "MultiPolygon":
        layer.renderer().setSymbol(QgsFillSymbol.createSimple({
            "style": "no", "outline_color": "#000000", "outline_width": "0.7",
            "outline_style": "dash"}))
    return layer


def pass_coverages(aoi_wkt: str, result: dict, passes: list[dict]) -> dict[int, Optional[float]]:
    """Fraction of the AOI covered by each pass: area(AOI ∩ ∪ footprints) / area(AOI).

    Areas are ellipsoidal (WGS 84). Tile percentages are never summed: overlapping
    tiles would count twice. A point AOI has no area fraction (returns {}); a pass
    without footprints gets None. Uses only geometry objects, so it may run in a task.
    """
    aoi = QgsGeometry.fromWkt(aoi_wkt)
    if aoi.isNull() or aoi.isEmpty() or "Point" in aoi.asWkt(0)[:12]:
        return {}
    aoi = aoi.makeValid()
    da = QgsDistanceArea()
    da.setSourceCrs(QgsCoordinateReferenceSystem(WGS84), QgsCoordinateTransformContext())
    da.setEllipsoid("WGS84")
    aoi_area = da.measureArea(aoi)
    if aoi_area <= 0:
        return {}
    geoms = {}
    for row, geom in join_rows(result):
        wkt = geometry_to_multipolygon_wkt(geom)
        if wkt:
            geoms[row.get("id")] = QgsGeometry.fromWkt(wkt).makeValid()
    out: dict[int, Optional[float]] = {}
    for p in passes:
        parts = [geoms[i] for i in p["row_ids"] if i in geoms]
        if not parts:
            out[p["id"]] = None
            continue
        inter = aoi.intersection(QgsGeometry.unaryUnion(parts))
        out[p["id"]] = (min(1.0, max(0.0, da.measureArea(inter) / aoi_area))
                        if not inter.isNull() else 0.0)
    return out
