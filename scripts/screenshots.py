"""Render the README figures offscreen from a real search on the public server.

    scripts/screenshots.sh            # English  -> docs/images/*.png
    scripts/screenshots.sh pt_BR      # Portuguese -> docs/images/pt-BR/*.png

The study area is the Paraibuna reservoir (São Paulo); the window runs from a week ago to ten
days ahead, so both catalogue scenes and forecasts appear. The base map is OpenStreetMap.
"""
import datetime as dt
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsCoordinateTransform, QgsMapRendererParallelJob, QgsMapSettings,
                       QgsProject, QgsRasterLayer, QgsRectangle)
from qgis.PyQt.QtCore import QDate, QLocale, QPoint, QRect, QSize, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from qgis.PyQt.QtWidgets import QGroupBox  # noqa: E402
from qgis.testing import start_app  # noqa: E402
from qgis.testing.mocked import get_iface  # noqa: E402

LANG = sys.argv[1] if len(sys.argv) > 1 else "en"
OUT = ROOT / "docs" / "images" / ("" if LANG == "en" else LANG)
LOCALE = "en_GB" if LANG == "en" else LANG  # day/month order, as most of the plugin's users read dates
AREA = QgsRectangle(-45.95, -23.62, -45.45, -23.28)  # Paraibuna reservoir
MISSIONS = {"sentinel-2", "landsat-8", "landsat-9", "cbers-4", "cbers-4a", "amazonia-1"}
BADGE = QColor("#1f5fbf")
OSM_CREDIT = "© OpenStreetMap contributors"


def wait_for(cond, timeout=300.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise TimeoutError
        QgsApplication.processEvents()
        time.sleep(0.05)


def badge(p: QPainter, x: int, y: int, text: str) -> None:
    """A numbered (or lettered) disc, the way the README refers to it."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("white"), 2))
    p.setBrush(BADGE)
    p.drawEllipse(QPoint(x, y), 13, 13)
    f = QFont("DejaVu Sans", 10)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRect(x - 13, y - 13, 26, 26), Qt.AlignmentFlag.AlignCenter, text)


def annotate(img: QImage, marks: list[tuple[int, int, str]]) -> QImage:
    p = QPainter(img)
    for x, y, t in marks:
        badge(p, x, y, t)
    p.end()
    return img


def grab(dock, page: str) -> QImage:
    dock.show_page(page)
    QgsApplication.processEvents()
    return dock.grab().toImage()


def at(widget, dock, dx=14, dy=12) -> tuple[int, int]:
    """Top-left corner of a child widget in dock coordinates, nudged inside."""
    pt = widget.mapTo(dock, QPoint(0, 0))
    return pt.x() + dx, pt.y() + dy


def render_map(layers, extent: QgsRectangle, size=QSize(900, 860)):
    """The rendered image, and the settings that place map coordinates on it."""
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(layers[-1].crs())
    ms.setExtent(extent)
    ms.setOutputSize(size)
    ms.setBackgroundColor(QColor("white"))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()
    p = QPainter(img)
    p.setFont(QFont("DejaVu Sans", 8))
    p.setPen(QColor("#333333"))
    p.drawText(img.rect().adjusted(0, 0, -6, -4), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
               OSM_CREDIT)
    p.end()
    return img, ms


def date_label(img: QImage, text: str) -> None:
    p = QPainter(img)
    f = QFont("DejaVu Sans", 11)
    f.setBold(True)
    p.setFont(f)
    p.fillRect(QRect(0, 0, 160, 26), QColor(255, 255, 255, 220))
    p.setPen(QColor("#1b2a3a"))
    p.drawText(QRect(8, 0, 160, 26), Qt.AlignmentFlag.AlignVCenter, text)
    p.end()


def side_by_side(images: list[QImage], gap=8) -> QImage:
    h = max(i.height() for i in images)
    out = QImage(sum(i.width() for i in images) + gap * (len(images) - 1), h, QImage.Format.Format_ARGB32)
    out.fill(QColor("white"))
    p = QPainter(out)
    x = 0
    for i in images:
        p.drawImage(x, 0, i)
        x += i.width() + gap
    p.end()
    return out


def main() -> None:
    app = start_app()
    QgsApplication.setFont(QFont("DejaVu Sans", 10))
    from swathcast_qgis.i18n import install_translator

    QLocale.setDefault(QLocale(LOCALE))
    app._translator = install_translator(LANG)  # kept: a collected translator stops translating
    # start_app runs on a temporary profile: this user's QGIS settings stay out of the figures

    from swathcast_qgis import dock as dock_mod

    QgsProject.instance().clear()
    dock = dock_mod.SwathcastDock(get_iface())
    dock.resize(400, 900)
    dock.show()
    wait_for(lambda: dock._loader is None)
    dock._check_missions(MISSIONS)
    dock._set_rect(AREA, "rectangle")
    today = dt.date.today()
    start, end = today - dt.timedelta(days=7), today + dt.timedelta(days=10)
    dock.date_start.setDate(QDate(start.year, start.month, start.day))
    dock.date_end.setDate(QDate(end.year, end.month, end.day))
    OUT.mkdir(parents=True, exist_ok=True)

    # the Search tab, with the four steps
    boxes = {b.title()[:2]: b for b in dock.findChildren(QGroupBox)}
    img = grab(dock, "search")
    marks = [(*at(boxes[n + "."], dock, dx=-2, dy=8), n) for n in "123"]
    marks.append((*at(dock.btn_run, dock, dx=-2, dy=8), "4"))
    annotate(img, marks).save(str(OUT / "search.png"))

    dock.run()
    wait_for(lambda: dock._running is None)
    if dock.layer is None:
        raise SystemExit(f"search failed: {dock.lbl_status.text()}")

    results = grab(dock, "results")
    results.save(str(OUT / "results.png"))

    # the map: OSM base, the area and the footprints of one day at a time (as the Temporal
    # Controller shows them), wide enough for a whole Sentinel-2 or Landsat swath
    osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19",
                         "OpenStreetMap", "wms")
    project = QgsProject.instance()
    aoi = next(lyr for lyr in project.mapLayers().values() if lyr is not dock.layer)
    to_map = QgsCoordinateTransform(aoi.crs(), QgsCoordinateReferenceSystem("EPSG:3857"), project)
    extent = to_map.transformBoundingBox(QgsRectangle(AREA.xMinimum() - 2.6, AREA.yMinimum() - 2.0,
                                                      AREA.xMaximum() + 2.6, AREA.yMaximum() + 2.0))
    narrow = {"sentinel-2", "landsat-8", "landsat-9"}
    by_day = {}
    for f in dock.layer.getFeatures():
        d = f["date"]
        d = d.toPyDate() if hasattr(d, "toPyDate") else d
        by_day.setdefault(d, set()).add(f["mission"])
    days = sorted(d for d, ms in by_day.items() if ms & narrow and d >= today) or sorted(by_day)

    def one_day(d, size):
        dock.layer.setSubsetString(f"\"date\" = '{d.isoformat()}'")
        img, ms = render_map([aoi, dock.layer, osm], extent, size)
        date_label(img, QLocale().toString(QDate(d.year, d.month, d.day), QLocale.FormatType.ShortFormat))
        return img, ms

    # the workspace: map + Results tab, panels A–D
    full, ms = one_day(days[0], QSize(900, results.height()))
    feat = next(dock.layer.getFeatures())
    c = to_map.transform(feat.geometry().pointOnSurface().asPoint())
    fx, fy = ms.mapToPixel().transform(c).x(), ms.mapToPixel().transform(c).y()
    a = to_map.transform(AREA.xMinimum(), AREA.yMaximum())
    ax, ay = ms.mapToPixel().transform(a).x(), ms.mapToPixel().transform(a).y()
    rail = dock.rail_buttons["results"].mapTo(dock, QPoint(0, 0))
    tree = dock.tree_res.mapTo(dock, QPoint(0, 0))
    off = full.width() + 8
    annotate(side_by_side([full, results]), [
        (off + rail.x() + 14, rail.y() + 10, "A"),
        (off + tree.x() + 14, tree.y() + 10, "B"),
        (int(fx), int(fy), "C"),
        (int(ax) - 14, int(ay) - 14, "D"),
    ]).save(str(OUT / "workspace.png"))

    # the Temporal Controller: three days
    panels = [one_day(d, QSize(420, 360))[0] for d in days[:3]]
    dock.layer.setSubsetString("")
    side_by_side(panels).save(str(OUT / "temporal.png"))

    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
    dock.unload()
    app.exitQgis()


if __name__ == "__main__":
    main()
