"""SwathCast for QGIS: when will a satellite image your study area?"""


def classFactory(iface):  # noqa: N802 — the name QGIS looks for
    from .plugin import SwathcastPlugin

    return SwathcastPlugin(iface)
