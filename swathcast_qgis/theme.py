"""Colours, icons and the stylesheet of the SwathCast dock.

Cool blues on the QGIS theme in use: blue and white in a dark theme, deep blue on
white in a light one. No orange anywhere (QGIS's Night Mapping focus colour is
orange; inside the dock it becomes blue). The stylesheet is scoped to the dock's
own widget, so the rest of QGIS keeps its look.
"""
from __future__ import annotations

import json
from pathlib import Path

from qgis.PyQt.QtCore import QByteArray, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QApplication

ROOT = "swathcastRoot"  # objectName of the dock's top widget; every rule hangs off it

DESIGN = Path(__file__).resolve().parent / "design"
#: the shared SwathCast tokens (copied from the swathcast-design folder by its sync script)
TOKENS = json.loads((DESIGN / "tokens.json").read_text(encoding="utf-8"))
DARK, LIGHT = TOKENS["color"]["dark"], TOKENS["color"]["light"]
RADIUS = TOKENS["radius"]
ICONS = (DESIGN / "icons").as_posix()  # QSS urls want forward slashes


def logo_path(dark: bool | None = None) -> str:
    """The SwathCast icon drawn for the theme: white satellite on dark, navy on light."""
    name = "dark" if (is_dark() if dark is None else dark) else "light"
    return str(Path(__file__).resolve().parent / f"swathcast_icon_{name}.svg")


def is_dark(palette: QPalette | None = None) -> bool:
    palette = palette or QApplication.palette()
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def colors(dark: bool | None = None) -> dict:
    return DARK if (is_dark() if dark is None else dark) else LIGHT


def icon(name: str, color: str, size: int = 48) -> QIcon:
    """A design-folder icon in ``color``, rendered sharp at ``size`` px (2x for HiDPI)."""
    svg = (DESIGN / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pm)
    renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", color).encode()))
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pm)


def rail_icon(name: str, c: dict) -> QIcon:
    """Muted when idle, accent blue when the tab is open."""
    ic = QIcon()
    for state, colour in ((QIcon.State.Off, c["muted"]), (QIcon.State.On, c["accent"])):
        pm = icon(name, colour).pixmap(48, 48)
        ic.addPixmap(pm, QIcon.Mode.Normal, state)
    return ic


def stylesheet(c: dict) -> str:
    r = f"#{ROOT}"
    return f"""
{r} QGroupBox {{
    background: {c['panel']}; border: 1px solid {c['panel_border']}; border-radius: {RADIUS['panel']}px;
    margin-top: 14px; padding: 8px 6px 6px 6px;
}}
{r} QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {c['title']}; font-weight: bold;
}}
{r} QPushButton {{
    background: {c['button']}; color: {c['button_text']}; font-weight: bold;
    border: 1px solid {c['button_border']}; border-radius: {RADIUS['button']}px; padding: 5px 8px;
    min-height: 18px;
}}
{r} QPushButton:hover {{ background: {c['button_hover']}; }}
{r} QPushButton:focus {{ border: 1px solid {c['accent']}; }}
{r} QPushButton:disabled {{
    background: transparent; color: {c['disabled']}; border-color: {c['panel_border']};
}}
{r} QPushButton#primary {{
    background: {c['primary']}; color: {c['primary_text']}; border: 1px solid {c['primary']};
    padding: 8px 14px; font-size: 11pt;
}}
{r} QPushButton#primary:hover {{ background: {c['primary_hover']}; }}
{r} QPushButton#primary:disabled {{
    background: {c['panel_border']}; color: {c['disabled']}; border-color: {c['panel_border']};
}}
{r} QPushButton#chip {{
    padding: 2px 9px; border-radius: {RADIUS['chip']}px; font-weight: normal; min-height: 14px;
}}
{r} QWidget#rail {{ background: {c['rail']}; border-radius: {RADIUS['panel']}px; }}
{r} QToolButton#railButton {{
    color: {c['muted']}; background: transparent; border: none;
    border-left: 3px solid transparent; border-radius: 0; padding: 6px 1px;
    font-size: 7.5pt;
}}
{r} QToolButton#railButton:hover {{ color: {c['text']}; }}
{r} QToolButton#railButton:checked {{
    color: {c['accent']}; background: {c['rail_checked']};
    border-left: 3px solid {c['accent']};
}}
{r} QComboBox {{
    background: {c['field']}; color: {c['text']}; border: 1px solid {c['panel_border']};
    border-radius: {RADIUS['field']}px; padding: 3px 6px;
}}
{r} QComboBox:disabled {{ color: {c['disabled']}; }}
{r} QLabel#appName {{ color: {c['text']}; font-size: 15pt; font-weight: bold; }}
{r} QLabel#versionPill {{
    background: {c['pill']}; color: {c['pill_text']}; border-radius: {RADIUS['pill']}px;
    padding: 1px 8px; font-size: 8pt; font-weight: bold;
}}
{r} QLabel#muted {{ color: {c['muted']}; }}
{r} QLabel#summary {{ color: {c['text']}; font-weight: bold; }}
{r} QCheckBox::indicator, {r} QTreeView::indicator {{
    width: 13px; height: 13px; border-radius: 3px;
    border: 1px solid {c['button_border']}; background: transparent;
}}
{r} QCheckBox::indicator:checked, {r} QTreeView::indicator:checked {{
    background: {c['check']}; border-color: {c['check']}; image: url({ICONS}/check.svg);
}}
{r} QTreeView::indicator:indeterminate {{
    background: {c['check']}; border-color: {c['check']}; image: url({ICONS}/partial.svg);
}}
{r} QCheckBox::indicator:disabled, {r} QTreeView::indicator:disabled {{
    border-color: {c['disabled']};
}}
{r} QLineEdit:focus, {r} QComboBox:focus, {r} QDateEdit:focus, {r} QSpinBox:focus,
{r} QTreeView:focus {{
    border: 1px solid {c['accent']};
}}
{r} QProgressBar {{
    border: 1px solid {c['panel_border']}; border-radius: 3px; max-height: 6px;
    background: {c['panel']};
}}
{r} QProgressBar::chunk {{ background: {c['primary']}; }}
{r} QTreeView::item:selected {{ background: {c['button']}; color: {c['button_text']}; }}
{r} QLineEdit, {r} QAbstractSpinBox, {r} QComboBox, {r} QAbstractItemView, {r} QLabel {{
    selection-background-color: {c['primary']}; selection-color: {c['primary_text']};
}}
"""


def apply(widget, dark: bool | None = None) -> dict:
    """Style ``widget`` (renamed to ROOT) for the current QGIS theme; return the colours."""
    c = colors(dark)
    widget.setObjectName(ROOT)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(stylesheet(c))
    return c
