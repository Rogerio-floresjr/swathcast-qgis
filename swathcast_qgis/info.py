"""The Info tab: what SwathCast is, how to use it, where forecasts come from, who made it."""
from __future__ import annotations

from html import escape
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget

from .welcome import DOCS_URL, SITE_URL, SOURCES_URL, welcome_sections

HERE = Path(__file__).resolve().parent
LOGO = HERE / "img" / "swathcast_logo.png"
AUTHOR = "Rogerio Flores Jr"
EMAIL = "rogerio.floresjr@gmail.com"
LOGO_WIDTH = 160  # logical px; the PNG is 640 px wide, so it stays sharp at 2x
translate = QCoreApplication.translate


def plugin_version() -> str:
    """``version=`` from metadata.txt, the one QGIS shows in the plugin manager."""
    try:
        for line in (HERE / "metadata.txt").read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _label(html: str) -> QLabel:
    lab = QLabel(html)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setOpenExternalLinks(True)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return lab


def _section(title: str, html: str) -> QGroupBox:
    box = QGroupBox(title)
    QVBoxLayout(box).addWidget(_label(html))
    return box


def _logo(ratio: float) -> QLabel:
    """The full logo on a white card, so it reads the same on dark and light themes."""
    lab = QLabel()
    lab.setObjectName("logoCard")
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet("#logoCard { background: #ffffff; border-radius: 8px; padding: 8px; }")
    pm = QPixmap(str(LOGO))
    if not pm.isNull():
        pm = pm.scaledToWidth(int(LOGO_WIDTH * ratio), Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(ratio)
        lab.setPixmap(pm)
    lab.setAccessibleName("SwathCast")
    return lab


def info_page(evidence: list[tuple[str, str]], ratio: float = 1.0,
              link_color: str = "") -> QWidget:
    """``evidence``: (label, explanation) pairs, as the results show them."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.addWidget(_logo(ratio))
    style = f" style='color:{link_color}'" if link_color else ""  # QLabel links ignore QSS
    author = escape(translate("InfoPage", "Author"))
    lay.addWidget(_label(f"<p align='center'>{author}: "
                         f"<b>{escape(AUTHOR)}</b><br><a href='mailto:{EMAIL}'{style}>{EMAIL}</a>"
                         "</p>"))

    lay.addWidget(_section(translate("InfoPage", "What it is"), escape(translate(
        "InfoPage",
        "SwathCast forecasts when a satellite will image your study area: Sentinel, Landsat, "
        "CBERS and Amazonia, MODIS, VIIRS, PACE and the tasked hyperspectral imagers. This "
        "plugin asks the public swathview server, which runs the SwathCast package, and adds "
        "the answer to the map as footprint layers that step through time."))))

    steps = [translate("InfoPage", "Search: choose the area, the satellites and the dates, "
                                   "then Find overpasses."),
             translate("InfoPage", "Results: passes by day; click one to highlight it, "
                                   "double-click to zoom."),
             translate("InfoPage", "Export the passes to a calendar (.ics), KML or CSV.")]
    lay.addWidget(_section(translate("InfoPage", "How to use"), "<ol>" + "".join(
        f"<li>{escape(s)}</li>" for s in steps) + "</ol>"))

    # a list, not a table: a two-column table is unreadable in a narrow dock
    lay.addWidget(_section(translate("InfoPage", "Where forecasts come from"), "".join(
        f"<p><b>{escape(name)}</b><br>{escape(text)}</p>" for name, text in evidence)))

    notes = welcome_sections()
    lay.addWidget(_section(translate("InfoPage", "Good to know"), "".join(
        f"<p><b>{escape(h)}.</b> {escape(p)}</p>" for h, p in notes)))

    docs = escape(translate("InfoPage", "Plugin documentation"))
    sources = escape(translate("InfoPage", "Data sources and methods"))
    privacy = escape(translate("InfoPage", "Privacy notice"))
    terms = escape(translate("InfoPage", "Terms of use"))
    # URL paths: each page exists in English and Portuguese (/privacidade, /termos)
    privacy_url = f"{SITE_URL}/{translate('InfoPage', 'privacy')}"
    terms_url = f"{SITE_URL}/{translate('InfoPage', 'terms')}"
    links = (f"<a href='{DOCS_URL}#qgis'{style}>{docs}</a><br>"
             f"<a href='{SOURCES_URL}'{style}>{sources}</a><br>"
             f"<a href='{privacy_url}'{style}>{privacy}</a><br>"
             f"<a href='{terms_url}'{style}>{terms}</a>")
    lay.addWidget(_section(translate("InfoPage", "Learn more"), links))
    lay.addStretch(1)
    return page
