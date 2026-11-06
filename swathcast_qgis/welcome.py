"""Welcome notice: a hobby project on free hosting, what is sent, where the data comes from."""
from __future__ import annotations

from html import escape

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

SITE_URL = "https://swathview.pages.dev"
DOCS_URL = SITE_URL + "/docs"
SOURCES_URL = DOCS_URL + "#sources"
# pylupdate6 finds strings in calls named translate("<literal context>", "<text>")
translate = QCoreApplication.translate
SEEN_KEY = "swathcast_qgis/welcome_seen"
WELCOME_VERSION = 2  # bump to show the notice again after its content changes


def welcome_sections() -> list[tuple[str, str]]:
    """(heading, paragraph) pairs, translated."""
    return [
        (translate("WelcomeDialog", "A hobby project"),
         translate("WelcomeDialog", "SwathCast is an independent hobby project, not affiliated with ESA, USGS, NASA, "
            "INPE, ASI or DLR. Its forecasts are estimates built from public data: they can be "
            "wrong and come with no guarantee. Do not use them for operational tasking or "
            "safety-critical planning; confirm with the satellite's operator.")),
        (translate("WelcomeDialog", "Free hosting"),
         translate("WelcomeDialog", "The calculations run on the public swathview server, which uses free services: "
            "Render for the server, Neon for the database and GitHub Actions for the nightly "
            "precompute. The server sleeps when nobody is using it, so the first search can "
            "take about a minute. To keep it free for everyone, searches are limited in dates "
            "and area size; the plugin warns you before sending one that is too large.")),
        (translate("WelcomeDialog", "What is sent"),
         translate("WelcomeDialog", "The area, the dates and the satellites you choose are sent to the server, which "
            "also receives your IP address and the plugin's name and version, as with any "
            "internet request. The server runs in the United States and keeps a search for one "
            "hour. Nothing else leaves QGIS.")),
        (translate("WelcomeDialog", "Where the forecasts come from"),
         translate("WelcomeDialog", "ESA acquisition plans (Sentinel-1, -2), the USGS calendar (Landsat), INPE's "
            "catalogue (CBERS, Amazonia), orbital elements from CelesTrak (Sentinel-3, MODIS, "
            "VIIRS, PACE, EnMAP, PRISMA) and the agencies' archive catalogues. Each result "
            "carries a label saying which of these it rests on.")),
    ]


def welcome_html() -> str:
    parts = [f"<p><b>{escape(h)}.</b> {escape(p)}</p>" for h, p in welcome_sections()]
    link = f'<a href="{SOURCES_URL}">{escape(translate("WelcomeDialog", "swathview documentation"))}</a>'
    parts.append("<p>" + escape(translate("WelcomeDialog", "Every data source, and how each forecast is made: {0}."))
                 .format(link) + "</p>")
    return "".join(parts)


class WelcomeDialog(QDialog):
    """``page``: the dock's Info tab content, shown as is; without it, plain text."""

    def __init__(self, parent=None, page: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("WelcomeDialog", "Welcome to SwathCast for QGIS"))
        lay = QVBoxLayout(self)
        body = None
        if page is not None:
            from . import theme

            self.resize(520, 680)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            body = QWidget()
            QVBoxLayout(body).addWidget(scroll)
            body.layout().setContentsMargins(0, 0, 0, 0)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(body)
        else:
            self.resize(560, 400)
            # a browser, not a word-wrapped QLabel: a label in a dialog gets the wrong height
            text = QTextBrowser()
            text.setOpenExternalLinks(True)
            text.setFrameShape(QFrame.Shape.NoFrame)
            text.viewport().setAutoFillBackground(False)
            text.setHtml(welcome_html())
            lay.addWidget(text)
        buttons = QDialogButtonBox()
        docs = buttons.addButton(translate("WelcomeDialog", "Data sources and methods"),
                                 QDialogButtonBox.ButtonRole.ActionRole)
        docs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SOURCES_URL)))
        ok = buttons.addButton(translate("WelcomeDialog", "Got it"), QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        if body is None:
            lay.addWidget(buttons)
        else:  # inside the styled body, so the buttons look like the dock's
            ok.setObjectName("primary")
            body.layout().addWidget(buttons)
            body.layout().setContentsMargins(8, 8, 8, 8)
            theme.apply(body)  # the same look as the dock's Info tab


def seen(settings: QgsSettings) -> bool:
    return int(settings.value(SEEN_KEY, 0) or 0) >= WELCOME_VERSION


def show_welcome(parent=None, settings: QgsSettings | None = None,
                 page: QWidget | None = None) -> None:
    """Show the notice (modal) and remember that it was seen."""
    WelcomeDialog(parent, page).exec()
    (settings or QgsSettings()).setValue(SEEN_KEY, WELCOME_VERSION)


def show_welcome_once(parent=None, page_factory=None) -> bool:
    """``page_factory``: builds the content (the dock's Info tab), only if shown."""
    settings = QgsSettings()
    if seen(settings):
        return False
    show_welcome(parent, settings, page_factory() if page_factory else None)
    return True
