"""The few QGIS/Qt API differences between QGIS 3.28+ (Qt5) and QGIS 4 (Qt6).

Every enum and constructor that moved between versions is resolved here once, so
the rest of the plugin reads the same on both.
"""
from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsField,
    QgsMapLayerProxyModel,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorLayerTemporalProperties,
    QgsWkbTypes,
)

QGIS_VERSION_INT = Qgis.QGIS_VERSION_INT


def _enum(new_owner, new_name: str, old_owner, old_name: str):
    """``new_owner.new_name`` when it exists, else the pre-3.30 spelling."""
    value = getattr(new_owner, new_name, None) if new_owner is not None else None
    return value if value is not None else getattr(old_owner, old_name)


if QGIS_VERSION_INT >= 33800:  # QgsField takes QMetaType.Type from 3.38 on
    from qgis.PyQt.QtCore import QMetaType

    _TYPES = {
        "str": QMetaType.Type.QString,
        "int": QMetaType.Type.Int,
        "float": QMetaType.Type.Double,
        "bool": QMetaType.Type.Bool,
        "datetime": QMetaType.Type.QDateTime,
        "date": QMetaType.Type.QDate,
    }
else:
    from qgis.PyQt.QtCore import QVariant

    _TYPES = {
        "str": QVariant.String,
        "int": QVariant.Int,
        "float": QVariant.Double,
        "bool": QVariant.Bool,
        "datetime": QVariant.DateTime,
        "date": QVariant.Date,
    }


def make_field(name: str, kind: str) -> QgsField:
    return QgsField(name, _TYPES[kind])


_LayerFilter = getattr(Qgis, "LayerFilter", None)
POLYGON_OR_POINT_LAYERS = (
    _enum(_LayerFilter, "PolygonLayer", QgsMapLayerProxyModel, "PolygonLayer")
    | _enum(_LayerFilter, "PointLayer", QgsMapLayerProxyModel, "PointLayer")
)
if hasattr(Qgis, "LayerFilters"):  # 3.34+: wrap so setFilters takes the new overload
    POLYGON_OR_POINT_LAYERS = Qgis.LayerFilters(POLYGON_OR_POINT_LAYERS)

_SymbolProperty = getattr(QgsSymbolLayer, "Property", None)
STROKE_STYLE_PROPERTY = _enum(_SymbolProperty, "StrokeStyle", QgsSymbolLayer, "PropertyStrokeStyle")

INSTANT_FROM_FIELD = _enum(
    getattr(Qgis, "VectorTemporalMode", None), "FeatureDateTimeInstantFromField",
    QgsVectorLayerTemporalProperties, "ModeFeatureDateTimeInstantFromField",
)
TEMPORAL_DAYS = _enum(getattr(Qgis, "TemporalUnit", None), "Days", QgsUnitTypes, "TemporalDays")

_GeometryType = getattr(Qgis, "GeometryType", None)
GEOMETRY_POINT = _enum(_GeometryType, "Point", QgsWkbTypes, "PointGeometry")
GEOMETRY_POLYGON = _enum(_GeometryType, "Polygon", QgsWkbTypes, "PolygonGeometry")


def soft_selection(layer, color) -> None:
    """Give ``layer`` its own selection colour instead of the project's (QGIS >= 3.34).

    The project default is opaque yellow; on footprints that are themselves
    coloured per mission it reads as a glare. Older QGIS keeps the project colour.
    """
    props = getattr(layer, "selectionProperties", None)
    mode = getattr(getattr(Qgis, "SelectionRenderingMode", None), "CustomColor", None)
    if props is None or mode is None:
        return
    props().setSelectionColor(color)
    props().setSelectionRenderingMode(mode)
