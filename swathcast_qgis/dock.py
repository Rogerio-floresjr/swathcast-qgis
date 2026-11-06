"""The SwathCast dock: pick an area, satellites and dates; see when they pass over it."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from osgeo import ogr
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeedback,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsTask,
)
from qgis.gui import (
    QgsCollapsibleGroupBox,
    QgsDockWidget,
    QgsMapLayerComboBox,
    QgsMapToolEmitPoint,
    QgsMapToolExtent,
    QgsRubberBand,
)
from qgis.PyQt.QtCore import QDate, QLocale, QObject, QSize, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import compat, theme
from .client import (
    DEFAULT_BASE_URL,
    NET_PROXY,
    WEB_URL,
    GROUP_ORDER,
    ApiError,
    Canceled,
    SwathviewClient,
    bbox_target,
    build_request,
    geojson_target,
    group_passes,
    limit_problems,
    mission_group,
    parse_web_link,
    point_target,
    web_link,
)
from .info import info_page, plugin_version
from .layers import (
    _utc,
    aoi_layer,
    footprint_layer,
    mission_color,
    pass_coverages,
    sensor_label,
    to_qdatetime,
)
from .transport import QgisTransport
from .widgets import FlowLayout, Spinner

SETTINGS = "swathcast_qgis/"
LOG_TAG = "swathcast"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MEDIUM_RES_OPTICAL = ("sentinel-2", "landsat-8", "landsat-9", "cbers-4", "cbers-4a",
                      "amazonia-1")
MAX_VERTICES = 2000  # larger AOI polygons are simplified before sending
SHARE_MAX_CHARS = 6000  # longer links break in e-mail and chat; polygons are simplified
#: the selection pulse: translucent white fading out (see _on_result_clicked)
_FLASH = theme.TOKENS["data"]["selection"]
FLASH_START = QColor(_FLASH["flash"])
FLASH_START.setAlphaF(_FLASH["flash_alpha"])
FLASH_END = QColor(_FLASH["flash"])
FLASH_END.setAlphaF(0)
FLASH_MS = _FLASH["flash_ms"]
RAIL_WIDTH = theme.TOKENS["size"]["rail_width"]
DATE_SHORTCUTS = (7, 15, 30)  # 'next n days' buttons under the dates

ICON_CIRCLE = getattr(QgsRubberBand, "ICON_CIRCLE", None)
if ICON_CIRCLE is None:
    ICON_CIRCLE = QgsRubberBand.IconType.ICON_CIRCLE
USER_ROLE = Qt.ItemDataRole.UserRole  # result rows: ("day", [pass ids]) / ("pass", id) / ("row", id)
EXPAND_ALL_MAX = 12  # expand every day only when the result is this short
CHECKED, UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked

AREA_POINT, AREA_RECT, AREA_EXTENT, AREA_LAYER = range(4)


class _Status(QObject):
    """Carries status codes from the worker thread to the GUI thread (queued)."""
    changed = pyqtSignal(str)


class _VerticalScroll(QScrollArea):
    """Scrolls up and down only, and asks for its page's full minimum width.

    A plain scroll area does not pass the page's minimum width on, so the dock
    could be made narrower than the form and cut it on the right.
    """

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        page = self.widget()
        if page is None:
            return hint
        width = (page.minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width()
                 + 2 * self.frameWidth())
        return QSize(max(hint.width(), width), hint.height())


def _remeasure(root: QWidget) -> None:
    """Drop every cached size under ``root`` once its stylesheet has been applied."""
    root.ensurePolished()
    layouts = root.findChildren(QLayout)
    for layout in layouts:
        layout.invalidate()
    for w in root.findChildren(QWidget):
        w.ensurePolished()
        w.updateGeometry()  # clears the widget's size cache in its parent layout
    for layout in layouts:
        layout.invalidate()


def _sleep_unless_canceled(task: QgsTask, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not task.isCanceled():
        time.sleep(0.2)


class SwathcastDock(QgsDockWidget):
    def __init__(self, iface, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SwathcastDock")
        self.setWindowTitle(self.tr("Satellite overpasses (SwathCast)"))
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.settings = QgsSettings()
        self.missions: list[dict] = []
        self.limits: dict = {}
        self.target: Optional[dict] = None
        self.target_geom: Optional[QgsGeometry] = None
        self.target_label = ""
        self.layer = None
        self.fids: dict[int, int] = {}
        self.job_id: Optional[str] = None
        self.passes: list[dict] = []
        self.last_req: Optional[dict] = None  # the request behind the results shown
        self._pending_sats: Optional[list[str]] = None  # from a link, before the list loads
        self._run_after_load = False
        self._link_sats: list[str] = []
        self._running = None  # (task, feedback, status bridge) of the active request
        self._generation = 0  # bumps per search; a late callback of an older one is ignored
        self._alive = True
        self._loader = None
        self._download = None
        self._prev_tool = None

        self.rb_area = QgsRubberBand(self.canvas, compat.GEOMETRY_POLYGON)
        self.rb_area.setStrokeColor(QColor(0, 0, 0))
        self.rb_area.setFillColor(QColor(255, 210, 0, 60))
        self.rb_area.setWidth(2)
        self.rb_point = QgsRubberBand(self.canvas, compat.GEOMETRY_POINT)
        self.rb_point.setIcon(ICON_CIRCLE)
        self.rb_point.setIconSize(14)
        self.rb_point.setColor(QColor(220, 30, 30))
        self.rb_point.setWidth(3)

        self.tool_point = QgsMapToolEmitPoint(self.canvas)
        self.tool_point.canvasClicked.connect(self._on_point)
        self.tool_rect = QgsMapToolExtent(self.canvas)
        self.tool_rect.extentChanged.connect(self._on_rect)

        self._build_ui()
        self._restore_settings()
        self._restore_missions()
        self.load_missions()

    # --- UI ----------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.colors = theme.colors()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addLayout(self._build_header())

        middle = QHBoxLayout()
        middle.setSpacing(4)
        rail = QWidget()
        rail.setObjectName("rail")
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(2)
        self.stack = QStackedWidget()
        self.rail_group = QButtonGroup(self)
        self.rail_buttons: dict[str, QToolButton] = {}
        pages = [("search", self.tr("Search"), self._with_run_row(self._build_search_page())),
                 ("results", self.tr("Results"), self._build_results_page()),
                 ("advanced", self.tr("Advanced"), self._scrolled(self._build_advanced_page())),
                 ("info", self.tr("Info"), self._scrolled(self.build_info_page()))]
        for i, (key, text, page) in enumerate(pages):
            self.stack.addWidget(page)
            b = QToolButton()
            b.setObjectName("railButton")
            b.setText(text)
            b.setAccessibleName(text)
            b.setIcon(theme.rail_icon(key, self.colors))
            b.setIconSize(QSize(22, 22))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setFixedWidth(RAIL_WIDTH)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.rail_group.addButton(b, i)
            self.rail_buttons[key] = b
            if key == "info":  # help sits at the foot of the rail
                rl.addStretch(1)
            rl.addWidget(b)
        self.btn_about = self.rail_buttons["info"]
        self.btn_about.setToolTip(self.tr("About this plugin: hobby project, free hosting, "
                                          "data sources"))
        middle.addWidget(rail)
        middle.addWidget(self.stack, 1)
        outer.addLayout(middle, 1)
        outer.addLayout(self._build_footer())

        # Styled last, then re-measured: layouts cache each widget's size from before
        # the stylesheet reached it (Qt's 80 px buttons), which held the dock ~140 px
        # wider than its styled contents.
        theme.apply(root)
        _remeasure(root)
        self.setWidget(root)
        self.show_page("search")
        self._on_area_mode(self.area_mode.currentIndex())

    def show_page(self, key: str) -> None:
        self.rail_buttons[key].setChecked(True)
        self.stack.setCurrentIndex(list(self.rail_buttons).index(key))

    @staticmethod
    def _scrolled(page: QWidget) -> QScrollArea:
        scroll = _VerticalScroll()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # a narrow dock wraps and shrinks the page instead of scrolling sideways
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _with_run_row(self, form: QWidget) -> QWidget:
        """The search form scrolls; its Find button stays pinned underneath it."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._scrolled(form), 1)
        row = QHBoxLayout()
        self.btn_run = QPushButton(self.tr("Find overpasses"))
        self.btn_run.setObjectName("primary")
        self.btn_run.setIcon(theme.icon("search", self.colors["primary_text"]))
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run, 1)
        lay.addLayout(row)
        return page

    @staticmethod
    def _chip(b: QPushButton) -> QPushButton:
        """A small rounded quick-pick button that may shrink to its text.

        Push buttons treat their size hint (about 80 px) as a minimum; a row of
        three short picks would then hold the whole dock wide.
        """
        b.setObjectName("chip")
        b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return b

    @staticmethod
    def _muted(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("muted")
        lab.setWordWrap(True)
        return lab

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(QIcon(theme.logo_path()).pixmap(QSize(30, 30)))
        logo.setAccessibleName("SwathCast")
        name = QLabel("SwathCast")
        name.setObjectName("appName")
        pill = QLabel("v" + plugin_version())
        pill.setObjectName("versionPill")
        pill.setToolTip(self.tr("Plugin version"))
        row.addWidget(logo)
        row.addWidget(name)
        row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        return row

    def _build_search_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.addWidget(self._muted(self.tr("When will a satellite image your study area? Choose "
                                          "the area, the satellites and the dates.")))

        # 1. area
        box = QGroupBox(self.tr("1. Area of interest"))
        bl = QVBoxLayout(box)
        self.area_mode = QComboBox()
        self.area_mode.addItems([self.tr("Click a point on the map"),
                                 self.tr("Drag a rectangle on the map"),
                                 self.tr("Current map extent"),
                                 self.tr("Features of a layer")])
        self.area_mode.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.area_mode.setMinimumContentsLength(8)
        self.area_mode.currentIndexChanged.connect(self._on_area_mode)
        bl.addWidget(self.area_mode)
        self.btn_area = QPushButton(self.tr("Choose"))
        self.btn_area.clicked.connect(self._choose_area)
        self.layer_row = QWidget()
        lr = QHBoxLayout(self.layer_row)
        lr.setContentsMargins(0, 0, 0, 0)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(compat.POLYGON_OR_POINT_LAYERS)
        self.chk_selected = QCheckBox(self.tr("selected only"))
        self.chk_selected.setChecked(True)
        lr.addWidget(self.layer_combo, 1)
        lr.addWidget(self.chk_selected)
        bl.addWidget(self.layer_row)
        row = QHBoxLayout()
        row.addWidget(self.btn_area)
        row.addStretch(1)
        bl.addLayout(row)
        self.lbl_area = self._muted(self.tr("No area chosen yet."))
        bl.addWidget(self.lbl_area)
        lay.addWidget(box)

        # 2. satellites
        box = QGroupBox(self.tr("2. Satellites"))
        bl = QVBoxLayout(box)
        self.sat_stack = QStackedWidget()
        self.sat_stack.setMinimumHeight(170)
        self.sat_stack.addWidget(self._build_sat_loading())
        self.tree_sat = QTreeWidget()
        self.tree_sat.setHeaderHidden(True)
        self.tree_sat.setTextElideMode(Qt.TextElideMode.ElideRight)  # the name matters, not km
        self.sat_stack.addWidget(self.tree_sat)
        bl.addWidget(self.sat_stack, 1)
        # quick picks on one line when they fit, wrapping only when the dock is narrow
        picks = QWidget()
        flow = FlowLayout(picks)
        for text, keys in ((self.tr("All"), None), (self.tr("None"), ()),
                           (self.tr("Medium-resolution optical"), MEDIUM_RES_OPTICAL)):
            b = self._chip(QPushButton(text))
            b.clicked.connect(lambda _=False, k=keys: self._check_missions(k))
            flow.addWidget(b)
        bl.addWidget(picks)
        lay.addWidget(box, 1)  # the satellite list takes the spare height

        # 3. dates
        box = QGroupBox(self.tr("3. Dates"))
        # a grid for the two dates and a plain column below: a form layout sizes a
        # spanning word-wrapped label by its unwrapped width
        bl = QVBoxLayout(box)
        grid = QGridLayout()
        self.date_start = QDateEdit()
        self.date_end = QDateEdit()
        for d in (self.date_start, self.date_end):
            d.setCalendarPopup(True)
            d.setDisplayFormat(QLocale().dateFormat(QLocale.FormatType.ShortFormat))
            d.dateChanged.connect(self._update_days)
        for i, (text, edit) in enumerate(((self.tr("From"), self.date_start),
                                          (self.tr("To (inclusive)"), self.date_end))):
            grid.addWidget(QLabel(text), i, 0)
            grid.addWidget(edit, i, 1)
        grid.setColumnStretch(1, 1)
        bl.addLayout(grid)
        row = QHBoxLayout()
        next_days = QLabel(self.tr("Next days:"))  # not word-wrapped: that widens the row
        next_days.setObjectName("muted")
        row.addWidget(next_days)
        for days in DATE_SHORTCUTS:
            b = QPushButton(str(days))
            b = self._chip(b)
            b.setToolTip(self.tr("From today, %n day(s)", "", days))
            b.setAccessibleName(b.toolTip())
            b.clicked.connect(lambda _=False, n=days: self._next_days(n))
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        self.lbl_days = self._muted("")
        bl.addWidget(self.lbl_days)
        bl.addWidget(self._muted(self.tr(
            "Past dates show the scenes found in the catalogues; future dates, the forecast.")))
        lay.addWidget(box)
        return page

    def _build_sat_loading(self) -> QWidget:
        """What the satellite box shows until the server has sent the list."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.sat_spinner = Spinner(self.colors["accent"])
        row.addWidget(self.sat_spinner)
        row.addStretch(1)
        lay.addLayout(row)
        self.lbl_sat_loading = self._muted(self.tr("Loading the satellite list…"))
        self.lbl_sat_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl_sat_loading)
        self.btn_retry = QPushButton(self.tr("Try again"))
        self.btn_retry.clicked.connect(self.load_missions)
        self.btn_retry.setVisible(False)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn_retry)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.lbl_summary = QLabel(self.tr("Nothing yet."))
        self.lbl_summary.setObjectName("summary")
        self.lbl_summary.setWordWrap(True)
        legend = QToolButton()
        legend.setIcon(theme.icon("info", self.colors["muted"]))
        legend.setAutoRaise(True)
        legend.setAccessibleName(self.tr("How to read the results"))
        # the legend is reference text: a tooltip, so the list keeps the height
        legend.setToolTip(self.tr(
            "Click a pass to highlight it on the map; double-click to zoom. Outline: dashed = "
            "cycle-projected, dotted = tasking required. The Temporal Controller steps "
            "through the days."))
        row.addWidget(self.lbl_summary, 1)
        row.addWidget(legend, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(row)
        self.tree_res = QTreeWidget()
        self.tree_res.setHeaderLabels([self.tr("Time"), self.tr("Pass"), self.tr("Area")])
        self.tree_res.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_res.setMinimumHeight(200)
        self.tree_res.setUniformRowHeights(False)
        header = self.tree_res.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_res.headerItem().setToolTip(2, self.tr(
            "Share of the area of interest covered by the pass (union of its footprints)."))
        self.tree_res.itemClicked.connect(self._on_result_clicked)
        self.tree_res.itemDoubleClicked.connect(self._on_result_double_clicked)
        lay.addWidget(self.tree_res, 1)
        self.lbl_temp = self._muted(self.tr(
            "These layers are temporary: they are lost when the project closes unless you make "
            "them permanent (the memory icon next to each layer)."))
        self.lbl_temp.setVisible(False)
        lay.addWidget(self.lbl_temp)
        row = QHBoxLayout()
        self.export_buttons = []
        for text, fmt in ((self.tr("Calendar"), "ical"), ("KML", "kml"), ("CSV", "csv")):
            b = QPushButton(text)
            if fmt == "ical":
                b.setToolTip(self.tr("Calendar file (.ics) for Google, Outlook or Apple "
                                     "calendars"))
            b.setEnabled(False)
            b.clicked.connect(lambda _=False, f=fmt: self.export(f))
            row.addWidget(b)
            self.export_buttons.append(b)
        box = QGroupBox(self.tr("Export and share"))
        bl = QVBoxLayout(box)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.btn_share = QPushButton(self.tr("Copy link"))
        self.btn_share.setToolTip(self.tr("A link to the same search in the swathview web app, "
                                          "to send to anyone; no QGIS needed to open it."))
        self.btn_share.clicked.connect(self.share)
        self.btn_web = QPushButton(self.tr("Open online"))
        self.btn_web.clicked.connect(lambda: self.share(open_browser=True))
        self.btn_web.setToolTip(self.tr("Opens the same search in the swathview web app."))
        for b in (self.btn_share, self.btn_web):
            b.setEnabled(False)
            row.addWidget(b)
        bl.addLayout(row)
        lay.addWidget(box)
        return page

    def _build_advanced_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 4, 0)
        box = QGroupBox(self.tr("Search options"))
        form = QFormLayout(box)
        self.chk_project = QCheckBox(self.tr("Project beyond the plans"))
        self.chk_project.setToolTip(self.tr(
            "Plans and calendars cover days to weeks. Beyond that, repeat the last known "
            "cycle (rows marked 'projected')."))
        form.addRow(self.chk_project)
        self.spin_cov = QSpinBox()
        self.spin_cov.setRange(0, 100)
        self.spin_cov.setSuffix(" %")
        self.spin_cov.setToolTip(self.tr("Drop passes that cover less of the area than this."))
        form.addRow(self.tr("Minimum coverage"), self.spin_cov)
        lay.addWidget(box)

        box = QGroupBox(self.tr("Open a link from the web app"))
        bl = QVBoxLayout(box)
        self.edit_link = QLineEdit()
        self.edit_link.setPlaceholderText(WEB_URL + "?t=point&lat=…")
        self.edit_link.setClearButtonEnabled(True)
        self.edit_link.returnPressed.connect(self.open_link)
        bl.addWidget(self.edit_link)
        row = QHBoxLayout()
        row.addWidget(self._muted(self.tr("Paste a swathview link to fill in the same search.")), 1)
        self.btn_link = QPushButton(self.tr("Open link"))
        self.btn_link.clicked.connect(self.open_link)
        row.addWidget(self.btn_link, 0, Qt.AlignmentFlag.AlignTop)
        bl.addLayout(row)
        lay.addWidget(box)

        box = QGroupBox(self.tr("Server"))
        form = QFormLayout(box)
        self.edit_url = QLineEdit()
        self.edit_url.setPlaceholderText(DEFAULT_BASE_URL)
        self.edit_key = QLineEdit()
        self.edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_key.setPlaceholderText(self.tr("not needed for the public server"))
        for w in (self.edit_url, self.edit_key):
            w.editingFinished.connect(self._save_server)
        form.addRow(self.tr("Server"), self.edit_url)
        form.addRow(self.tr("API key"), self.edit_key)
        form.addRow(self._muted(self.tr("The server receives the area, the dates, the satellites "
                                        "and your IP address.")))
        self.btn_reload = QPushButton(self.tr("Reload satellites"))
        self.btn_reload.clicked.connect(self.load_missions)
        form.addRow(self.btn_reload)
        lay.addWidget(box)
        lay.addStretch(1)
        return page

    def build_info_page(self) -> QWidget:
        """The Info tab's content; the welcome notice shows the same."""
        labels, helps = self.evidence_labels(), self.evidence_help()
        evidence = [(labels[k], helps[k]) for k in labels if k != "unspecified"]
        evidence.append((self.tr("tasking required"), self.tr(
            "Tasking required: the geometry allows imaging, but an image exists only if "
            "someone orders it.")))
        return info_page(evidence, self.devicePixelRatioF(), self.colors["link"])

    def _build_footer(self) -> QVBoxLayout:
        """Under every tab: progress and status (link, share and export messages too)."""
        foot = QVBoxLayout()
        foot.setSpacing(4)
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        # beside the progress, on every tab: a cold server can take a minute
        self.btn_cancel = QPushButton(self.tr("Stop waiting"))
        self.btn_cancel.setToolTip(self.tr("Stops waiting for the server; the server may still "
                                           "finish the calculation."))
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_cancel.setVisible(False)
        row.addWidget(self.progress, 1)
        row.addWidget(self.btn_cancel)
        foot.addLayout(row)
        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        foot.addWidget(self.lbl_status)
        return foot

    def _restore_settings(self) -> None:
        s = self.settings
        today = QDate.currentDate()
        days = int(s.value(SETTINGS + "window_days", 21))
        self.date_start.setDate(today)
        self.date_end.setDate(today.addDays(days - 1))
        self.chk_project.setChecked(s.value(SETTINGS + "project", "true") == "true")
        self.spin_cov.setValue(int(s.value(SETTINGS + "min_coverage", 0)))
        self.area_mode.setCurrentIndex(int(s.value(SETTINGS + "area_mode", AREA_RECT)))
        self.edit_url.setText(s.value(SETTINGS + "base_url", "") or "")
        self.edit_key.setText(s.value(SETTINGS + "api_key", "") or "")
        self._update_days()

    def _save_run_settings(self) -> None:
        s = self.settings
        s.setValue(SETTINGS + "window_days", self._days())
        s.setValue(SETTINGS + "project", "true" if self.chk_project.isChecked() else "false")
        s.setValue(SETTINGS + "min_coverage", self.spin_cov.value())
        s.setValue(SETTINGS + "area_mode", self.area_mode.currentIndex())
        s.setValue(SETTINGS + "satellites", ",".join(self.checked_missions()))

    def _save_server(self) -> None:
        self.settings.setValue(SETTINGS + "base_url", self.edit_url.text().strip())
        self.settings.setValue(SETTINGS + "api_key", self.edit_key.text().strip())

    def _client(self, feedback: Optional[QgsFeedback] = None) -> SwathviewClient:
        return SwathviewClient(QgisTransport(self.edit_key.text().strip(), feedback),
                               self.edit_url.text().strip() or DEFAULT_BASE_URL)

    def _days(self) -> int:
        return self.date_start.date().daysTo(self.date_end.date()) + 1

    def _update_days(self) -> None:
        days = self._days()
        text = self.tr("%n day(s)", "", days) if days > 0 else self.tr("the end is before the start")
        max_days = self.limits.get("max_window_days")
        if max_days:
            text += " " + self.tr("(this server allows up to {0})").format(max_days)
        self.lbl_days.setText(text)

    def _next_days(self, days: int) -> None:
        today = QDate.currentDate()
        self.date_start.setDate(today)
        self.date_end.setDate(today.addDays(days - 1))

    def _set_status(self, text: str, error: bool = False) -> None:
        self.lbl_status.setStyleSheet(f"color: {self.colors['error']};" if error else "")
        self.lbl_status.setText(text)

    # --- satellites ----------------------------------------------------------

    def _restore_missions(self) -> None:
        """Show the last satellite list at once; the server's reply refreshes it.

        A free server that has gone to sleep takes up to a minute to answer: the
        form stays usable meanwhile instead of showing an empty list.
        """
        try:
            cached = json.loads(self.settings.value(SETTINGS + "missions_cache", "") or "{}")
        except (TypeError, ValueError):
            return
        if cached.get("url") != self._server_url() or not cached.get("missions"):
            return
        self.missions, self.limits = cached["missions"], cached.get("limits") or {}
        self._fill_missions()
        self.sat_stack.setCurrentIndex(1)
        self._update_days()

    def _server_url(self) -> str:
        return self.edit_url.text().strip() or DEFAULT_BASE_URL

    def load_missions(self) -> None:
        if self._loader is not None:
            return
        self.btn_retry.setVisible(False)
        self.btn_run.setEnabled(bool(self.missions))  # a remembered list can already search
        self.sat_spinner.setVisible(True)
        self.lbl_sat_loading.setText(self.tr("Loading the satellite list…"))
        if not self.missions:
            self.sat_stack.setCurrentIndex(0)
        self._set_status(self.tr("Loading the satellite list…"))
        feedback = QgsFeedback()
        status = _Status()
        status.changed.connect(self._on_status)
        client = self._client(feedback)

        def work(task):
            health = client.wake(status.changed.emit, task.isCanceled,
                                 sleep=lambda s: _sleep_unless_canceled(task, s))
            return health, client.missions()

        task = QgsTask.fromFunction(self.tr("Loading SwathCast satellites"), work,
                                    on_finished=self._on_missions_loaded)
        self._loader = (task, feedback, status)
        QgsApplication.taskManager().addTask(task)

    def _on_missions_loaded(self, exception, value=None) -> None:
        self._loader = None
        if not self._alive:
            return
        if exception is not None or value is None:
            self._run_after_load = False
            self.sat_spinner.setVisible(False)
            # with a remembered list the tree stays; the status line says what failed
            self.lbl_sat_loading.setText(self.tr("Could not load the satellite list."))
            self._set_status(self._error_text(exception), error=True)
            self.btn_retry.setVisible(True)
            return
        health, missions = value
        self.limits = health.get("limits") or {}
        self.missions = missions
        self.settings.setValue(SETTINGS + "missions_cache", json.dumps(
            {"url": self._server_url(), "missions": missions, "limits": self.limits}))
        self._fill_missions()
        self.sat_stack.setCurrentIndex(1)
        self._update_days()
        self.btn_run.setEnabled(True)
        self._set_status(self.tr("Ready."))
        if self._run_after_load:  # a link opened while the list was loading
            self._run_after_load = False
            self._run_link_search()

    def _fill_missions(self) -> None:
        saved = self.settings.value(SETTINGS + "satellites", None)
        wanted = set(saved.split(",")) if saved else set(MEDIUM_RES_OPTICAL)
        if self._pending_sats is not None:
            wanted, self._pending_sats = set(self._pending_sats), None
        elif self.tree_sat.topLevelItemCount():  # a refresh keeps what the user ticked
            wanted = set(self.checked_missions())
        acquisition = {
            "plan": self.tr("operator acquisition plan"),
            "systematic": self.tr("systematic: images every pass"),
            "tasking": self.tr("on request only (tasking): shows opportunities"),
        }
        self.tree_sat.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for m in sorted(self.missions, key=lambda m: m["long_name"]):
            g = mission_group(m["key"])
            if g not in groups:
                groups[g] = QTreeWidgetItem([self._group_label(g)])
                groups[g].setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                                   | Qt.ItemFlag.ItemIsAutoTristate)
            item = QTreeWidgetItem(groups[g], [self._mission_label(m)])
            item.setData(0, USER_ROLE, m["key"])
            item.setIcon(0, self._swatch(m["key"]))
            item.setToolTip(0, m["long_name"] + "\n"
                            + acquisition.get(m.get("acquisition"), m.get("acquisition", "")))
            flags = Qt.ItemFlag.ItemIsUserCheckable
            if m.get("enabled", True):
                flags |= Qt.ItemFlag.ItemIsEnabled
            else:
                item.setToolTip(0, self.tr("Not available on this server."))
            item.setFlags(flags)
            item.setCheckState(0, CHECKED if m["key"] in wanted and m.get("enabled", True)
                               else UNCHECKED)
        for g in GROUP_ORDER:
            if g in groups:
                self.tree_sat.addTopLevelItem(groups[g])
                groups[g].setExpanded(g in ("INPE", "Sentinel", "Landsat"))

    def _group_label(self, group: str) -> str:
        """Display name of a mission group (the key stays the internal id).

        PACE OCI is hyperspectral too, but images continuously at ~1 km; EnMAP and
        PRISMA are the tasked 30 m hyperspectral imagers. The labels say so.
        """
        return {"PACE": self.tr("PACE (hyperspectral)"),
                "Hyperspectral": self.tr("Hyperspectral (tasked)"),
                "Other": self.tr("Other")}.get(group, group)

    @staticmethod
    def _mission_label(m: dict) -> str:
        """'CBERS-4 WFI (866 km)', 'Sentinel-2 — MSI (290 km)'.

        The programme prefix goes: in a narrow dock 'Copernicus Sentinel-…' hid which
        Sentinel a row was. The full name is in the row's tooltip.
        """
        name, parts = m["long_name"].removeprefix("Copernicus "), []
        for s in m.get("sensors", []):
            swath = f" ({s['swath_km']:.0f} km)" if s.get("swath_km") else ""
            if s["name"] in name.split():
                name += swath
            else:
                parts.append(s["name"] + swath)
        return f"{name} — {', '.join(parts)}" if parts else name

    @staticmethod
    def _swatch(key: str) -> QIcon:
        pm = QPixmap(12, 12)
        pm.fill(QColor(mission_color(key)))
        return QIcon(pm)

    def _mission_items(self):
        for i in range(self.tree_sat.topLevelItemCount()):
            group = self.tree_sat.topLevelItem(i)
            for j in range(group.childCount()):
                yield group.child(j)

    def checked_missions(self) -> list[str]:
        return [it.data(0, USER_ROLE) for it in self._mission_items()
                if it.checkState(0) == CHECKED]

    def _check_missions(self, keys) -> None:
        for it in self._mission_items():
            on = keys is None or it.data(0, USER_ROLE) in keys
            if not (it.flags() & Qt.ItemFlag.ItemIsEnabled):
                on = False
            it.setCheckState(0, CHECKED if on else UNCHECKED)

    # --- area ----------------------------------------------------------------

    def _on_area_mode(self, mode: int) -> None:
        self.layer_row.setVisible(mode == AREA_LAYER)
        self.btn_area.setText(self.tr("Use") if mode in (AREA_EXTENT, AREA_LAYER)
                              else self.tr("Choose on map"))

    def _to_wgs84(self, crs) -> QgsCoordinateTransform:
        return QgsCoordinateTransform(crs, WGS84, QgsProject.instance())

    def _choose_area(self) -> None:
        mode = self.area_mode.currentIndex()
        if mode == AREA_POINT:
            self._activate_tool(self.tool_point)
            self.iface.messageBar().pushInfo("SwathCast", self.tr("Click the map to set the point."))
        elif mode == AREA_RECT:
            self._activate_tool(self.tool_rect)
            self.iface.messageBar().pushInfo("SwathCast", self.tr("Drag a rectangle on the map."))
        elif mode == AREA_EXTENT:
            self._set_rect(self._to_wgs84(self.canvas.mapSettings().destinationCrs())
                           .transformBoundingBox(self.canvas.extent()),
                           self.tr("map extent"))
        else:
            self._use_layer()

    def _activate_tool(self, tool) -> None:
        if self.canvas.mapTool() not in (self.tool_point, self.tool_rect):
            self._prev_tool = self.canvas.mapTool()
        self.canvas.setMapTool(tool)

    def _restore_tool(self) -> None:
        if self._prev_tool is not None:
            self.canvas.setMapTool(self._prev_tool)
        else:
            self.canvas.unsetMapTool(self.canvas.mapTool())
        self._prev_tool = None

    def _on_point(self, point: QgsPointXY, _button) -> None:
        p = self._to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(point)
        self._set_point(p)
        self._restore_tool()

    def _on_rect(self, rect: QgsRectangle) -> None:
        self._restore_tool()
        if rect.isEmpty():
            return
        self._set_rect(self._to_wgs84(self.canvas.mapSettings().destinationCrs())
                       .transformBoundingBox(rect), self.tr("rectangle"))

    def _set_point(self, p: QgsPointXY) -> None:
        loc = QLocale()
        label = self.tr("Point {0}, {1}").format(loc.toString(p.y(), "f", 4),
                                                  loc.toString(p.x(), "f", 4))
        self._set_target(point_target(p.y(), p.x()), QgsGeometry.fromPointXY(p), label)

    def _set_rect(self, r: QgsRectangle, what: str) -> None:
        w, s = max(-180.0, r.xMinimum()), max(-90.0, r.yMinimum())
        e, n = min(180.0, r.xMaximum()), min(90.0, r.yMaximum())
        if e - w < 1e-6 or n - s < 1e-6:
            self._set_point(QgsPointXY((w + e) / 2, (s + n) / 2))
            return
        loc = QLocale()
        label = self.tr("{0}: {1}° × {2}° ({3} square degrees)").format(
            what.capitalize(), loc.toString(e - w, "f", 2), loc.toString(n - s, "f", 2),
            loc.toString((e - w) * (n - s), "f", 2))
        self._set_target(bbox_target(w, s, e, n),
                         QgsGeometry.fromRect(QgsRectangle(w, s, e, n)), label)

    def _use_layer(self) -> None:
        layer = self.layer_combo.currentLayer()
        if layer is None:
            self._set_status(self.tr("Add a point or polygon layer to the project first."), True)
            return
        if self.chk_selected.isChecked():
            if layer.selectedFeatureCount() == 0:
                self._set_status(self.tr("No features are selected in '{0}'. Select some, or "
                                         "untick 'selected only'.").format(layer.name()), True)
                return
            feats = layer.selectedFeatures()
        else:
            feats = layer.getFeatures()
        geoms = [f.geometry() for f in feats if f.hasGeometry()]
        if not geoms:
            self._set_status(self.tr("'{0}' has no geometries to use.").format(layer.name()), True)
            return
        geom = QgsGeometry.unaryUnion(geoms)
        geom.transform(self._to_wgs84(layer.crs()))
        gj = json.loads(geom.asJson(6))
        kind = gj.get("type")
        if kind == "Point":
            self._set_point(geom.asPoint())
            return
        if kind not in ("Polygon", "MultiPolygon"):
            self._set_rect(geom.boundingBox(), layer.name())
            return
        if sum(1 for _ in geom.vertices()) > MAX_VERTICES:
            geom = geom.simplify(0.001)
            gj = json.loads(geom.asJson(6))
        bb = geom.boundingBox()
        loc = QLocale()
        label = self.tr("{0}: {1} feature(s), {2}° × {3}°").format(
            layer.name(), len(geoms), loc.toString(bb.width(), "f", 2),
            loc.toString(bb.height(), "f", 2))
        self._set_target(geojson_target(gj), geom, label)

    def _set_target(self, target: dict, geom: QgsGeometry, label: str) -> None:
        self.target, self.target_geom, self.target_label = target, geom, label
        self.lbl_area.setText(label)
        self.rb_area.reset(compat.GEOMETRY_POLYGON)
        self.rb_point.reset(compat.GEOMETRY_POINT)
        if target["type"] == "point":
            self.rb_point.addPoint(self._from_wgs84(geom.asPoint()))
        else:
            self.rb_area.setToGeometry(geom, WGS84)
        self._set_status("")

    def _from_wgs84(self, p: QgsPointXY) -> QgsPointXY:
        xf = QgsCoordinateTransform(WGS84, self.canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        return xf.transform(p)

    # --- run -----------------------------------------------------------------

    def run(self) -> None:
        if self._running is not None:
            return
        if self.target is None:
            self._set_status(self.tr("Choose an area of interest first (step 1)."), True)
            return
        start = self.date_start.date().toPyDate()
        # "To" is inclusive for the user; the server's end is an exclusive instant.
        end = self.date_end.date().toPyDate() + timedelta(days=1)
        req = build_request(self.target, self.checked_missions(), start, end,
                            project=self.chk_project.isChecked(),
                            min_coverage=self.spin_cov.value() / 100.0)
        tasking = {m["key"] for m in self.missions if m.get("acquisition") == "tasking"}
        problems = limit_problems(req, self.limits, tasking)
        if problems:
            self._set_status("\n".join(self._problem_text(c, p) for c, p in problems), True)
            return
        self._save_run_settings()

        feedback = QgsFeedback()
        status = _Status()
        status.changed.connect(self._on_status)
        client = self._client(feedback)
        aoi_wkt = self.target_geom.asWkt(7)
        self._generation += 1
        generation = self._generation

        def work(task):
            job_id, result = client.run(req, status.changed.emit, task.isCanceled,
                                        sleep=lambda s: _sleep_unless_canceled(task, s))
            status.changed.emit("preparing")
            # grouping and union areas are plain geometry work: keep them off the GUI thread
            passes = group_passes(result.get("overpasses") or [])
            coverage = pass_coverages(aoi_wkt, result, passes)
            for p in passes:
                p["coverage"] = coverage.get(p["id"])
            return job_id, result, passes

        task = QgsTask.fromFunction(
            self.tr("Predicting satellite overpasses"), work,
            on_finished=lambda exc, value=None: self._on_done(generation, req, exc, value))
        self._running = (task, feedback, status)
        self._busy(True)
        QgsApplication.taskManager().addTask(task)

    def cancel(self) -> None:
        if self._running is not None:
            task, feedback, _ = self._running
            feedback.cancel()
            task.cancel()

    def _busy(self, busy: bool) -> None:
        self.btn_run.setEnabled(not busy)
        self.btn_cancel.setVisible(busy)
        self.progress.setVisible(busy)

    def _on_status(self, code: str) -> None:
        text = {
            "connecting": self.tr("Connecting to the server…"),
            "waking": self.tr("The free public server may be starting; this can take about a "
                              "minute."),
            "submitting": self.tr("Sending the request…"),
            "computing": self.tr("Computing the overpasses…"),
            "preparing": self.tr("Grouping records into passes…"),
        }.get(code, code)
        self._set_status(text)

    def _problem_text(self, code: str, p: dict) -> str:
        loc = QLocale()
        return {
            "no_satellites": self.tr("Tick at least one satellite (step 2)."),
            "bad_window": self.tr("The end date must not be before the start date."),
            "window_too_long": self.tr("The period has {0} days; this server allows up to {1}. "
                                       "Split it in parts.").format(p.get("days"), p.get("max")),
            "aoi_too_big": self.tr("The area spans {0} square degrees; this server allows up to "
                                   "{1}. Choose a smaller area or split it.").format(
                loc.toString(float(p.get("area", 0)), "f", 1), p.get("max")),
            "tasking_aoi_too_big": self.tr(
                "{0} are computed on request, so this server takes them only for a point or "
                "an area up to {1} square degrees.").format(p.get("missions"), p.get("max")),
        }.get(code, code)

    def _error_text(self, exc) -> str:
        if isinstance(exc, Canceled):
            if self.passes:
                return self.tr("Stopped waiting. The previous result is still available.")
            return self.tr("Stopped waiting.")
        if isinstance(exc, ApiError):
            if exc.status == NET_PROXY:
                return self.tr("QGIS's proxy refused the connection ({0}). Check the proxy under "
                               "Settings › Options › Network, or untick it if this network "
                               "has none.").format(exc)
            if exc.status is not None and exc.status <= 0:
                return self.tr("Could not reach the server ({0}). Check the internet connection, "
                               "or the server address under Advanced.").format(exc)
            if exc.status == 401:
                return self.tr("This server needs an API key (Advanced).")
            if exc.status == 404:
                return self.tr("The result has expired on the server; run the search again.")
            if exc.status == 429:
                return self.tr("Too many searches from this network in the last minute; wait a "
                               "minute and try again.")
            if exc.status in (400, 422):
                return self.tr("The server refused the request: {0}").format(exc)
            return self.tr("Server error: {0}").format(exc)
        QgsMessageLog.logMessage(repr(exc), LOG_TAG)
        return self.tr("Unexpected error: {0}").format(exc)

    def _on_done(self, generation: int, req: dict, exception, value) -> None:
        if not self._alive or generation != self._generation:
            return  # the dock was unloaded, or a newer search owns the panel
        task = self._running[0] if self._running else None
        self._running = None
        self._busy(False)
        if exception is not None or value is None:
            if task is not None and task.isCanceled():
                exception = Canceled()
            self._set_status(self._error_text(exception), True)
            return
        self.job_id, result, passes = value
        try:
            self._show_result(req, result, passes)
        except Exception as exc:  # noqa: BLE001 — report, never crash QGIS
            QgsMessageLog.logMessage(repr(exc), LOG_TAG)
            self._set_status(self.tr("Could not draw the result: {0}").format(exc), True)

    # --- results -------------------------------------------------------------

    def evidence_labels(self) -> dict:
        return {
            "in_catalogue": self.tr("In catalogue"),
            "planned": self.tr("Planned"),
            "operator_forecast": self.tr("Operator forecast"),
            "archive_derived": self.tr("Archive-derived"),
            "cycle_projected": self.tr("Cycle-projected"),
            "orbit_derived": self.tr("Orbit-derived"),
            "unspecified": self.tr("Source unspecified"),
        }

    def evidence_help(self) -> dict:
        return {
            "in_catalogue": self.tr("A matching product was found in the catalogue."),
            "planned": self.tr("Listed in the operator's acquisition plan or calendar."),
            "operator_forecast": self.tr("The operator's own feasibility forecast."),
            "archive_derived": self.tr("Expected from the recurring pattern of past images in "
                                       "INPE's catalogue; not an operator plan."),
            "cycle_projected": self.tr("Extrapolated from the last known repeat cycle; valid "
                                       "while the constellation does not change."),
            "orbit_derived": self.tr("Calculated from the orbit: the sensor can see the area "
                                     "on this pass."),
            "unspecified": self.tr("The server did not say where this forecast comes from."),
        }

    def _coverage_text(self, fraction: Optional[float]) -> str:
        """Area share for display: '<1 %' for tiny overlaps, never a fake 0 %."""
        if self.target and self.target["type"] == "point":
            return self.tr("point inside")
        if fraction is None:
            return "—"
        if fraction <= 0:
            return self.tr("edge only")
        pct = fraction * 100
        return "<1 %" if pct < 1 else f"{pct:.0f} %"

    def _pass_text(self, p: dict, labels: dict) -> str:
        """Two lines: satellite/sensor, then evidence · tasking · records · cloud."""
        parts = [labels.get(p["evidence"], p["evidence"])]
        if p["tasking_required"]:
            parts.append(self.tr("tasking required"))
        n = len(p["row_ids"])
        if n > 1:
            parts.append(self.tr("%n record(s)", "", n))
        if p.get("cloud_range"):
            lo, hi = p["cloud_range"]
            parts.append(self.tr("cloud {0}").format(
                f"{lo:.0f} %" if round(lo) == round(hi) else f"{lo:.0f}–{hi:.0f} %"))
        return f"{sensor_label(p['satellite'] or '', p['sensor'] or '')}\n" + " · ".join(parts)

    def _show_result(self, req: dict, result: dict, passes: list[dict]) -> None:
        rows = result.get("overpasses") or []
        self.last_req = req
        for b in self.export_buttons:
            b.setEnabled(bool(rows))
        for b in (self.btn_share, self.btn_web):
            b.setEnabled(True)  # an empty answer is worth sharing too
        self.tree_res.clear()
        self.fids = {}
        self.passes = passes
        loc = QLocale()
        period = f"{loc.toString(self.date_start.date(), QLocale.FormatType.ShortFormat)} – " \
                 f"{loc.toString(self.date_end.date(), QLocale.FormatType.ShortFormat)}"
        if not rows:
            self.lbl_summary.setText(self.tr("No overpasses found for this area and period."))
            self.lbl_temp.setVisible(False)
            self._set_status(self.tr("Done."))
            return

        names = {m["key"]: m["long_name"] for m in self.missions}
        labels = self.evidence_labels()
        helps = self.evidence_help()
        pass_of = {rid: p["id"] for p in passes for rid in p["row_ids"]}
        layer, fids = footprint_layer(result, self.tr("Overpasses {0}").format(period),
                                      labels, names, pass_of)
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, f"SwathCast · {period}")
        aoi = aoi_layer(self.target_geom, self.tr("Area of interest"))
        project.addMapLayer(aoi, False)
        group.addLayer(aoi)
        project.addMapLayer(layer, False)
        group.addLayer(layer)
        self.layer, self.fids = layer, fids
        layer.willBeDeleted.connect(self._forget_layer)
        # the area layer takes over from the temporary drawing
        self.rb_area.reset(compat.GEOMETRY_POLYGON)
        self.rb_point.reset(compat.GEOMETRY_POINT)

        rows_by_id = {r.get("id"): r for r in rows}
        by_day: dict = {}
        for p in passes:
            ts = _utc(p["start"])
            if ts is None:
                continue
            by_day.setdefault(to_qdatetime(ts).toLocalTime().date().toString("yyyy-MM-dd"),
                              []).append(p)
        expand_all = len(passes) <= EXPAND_ALL_MAX
        for day_passes in by_day.values():
            qd = to_qdatetime(_utc(day_passes[0]["start"])).toLocalTime().date()
            # one spanned line per day leaves the Time column as narrow as a time
            top = QTreeWidgetItem([loc.toString(qd, "ddd dd/MM") + " · "
                                   + self.tr("%n pass(es)", "", len(day_passes))])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setData(0, USER_ROLE, ("day", [p["id"] for p in day_passes]))
            self.tree_res.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)
            for p in day_passes:
                t0 = to_qdatetime(_utc(p["start"])).toLocalTime().toString("HH:mm")
                t1 = to_qdatetime(_utc(p["end"])).toLocalTime().toString("HH:mm")
                item = QTreeWidgetItem(top, [t0, self._pass_text(p, labels),
                                             self._coverage_text(p.get("coverage"))])
                if t1 != t0:
                    item.setToolTip(0, f"{t0}–{t1}")
                item.setIcon(1, self._swatch(p["mission"] or ""))
                item.setData(0, USER_ROLE, ("pass", p["id"]))
                tip = [helps.get(p["evidence"], "")]
                if p["tasking_required"]:
                    tip.append(self.tr("Tasking required: the geometry allows imaging, but an "
                                       "image exists only if someone orders it."))
                if p.get("projected_from"):
                    tip.append(self.tr("Projected from {0}").format(p["projected_from"]))
                if p.get("grouped_by_time"):
                    tip.append(self.tr("Records grouped by time: same satellite within half an "
                                       "orbit."))
                item.setToolTip(1, "\n".join(t for t in tip if t))
                if len(p["row_ids"]) > 1:
                    for rid in p["row_ids"]:
                        r = rows_by_id.get(rid, {})
                        ts = _utc(r.get("timestamp_utc"))
                        child = QTreeWidgetItem(item, [
                            to_qdatetime(ts).toLocalTime().toString("HH:mm") if ts else "",
                            str(r.get("grid_cell") or r.get("product_id") or rid),
                            self._coverage_text(r.get("coverage_fraction")),
                        ])
                        child.setData(0, USER_ROLE, ("row", rid))
            top.setExpanded(expand_all or self.tree_res.topLevelItemCount() == 1)

        text = " · ".join([
            self.tr("%n pass(es)", "", len(passes)),
            self.tr("%n record(s)", "", len(rows)),
            self.tr("{0} of %n day(s) with passes", "", self._days()).format(len(by_day)),
        ])
        cov = result.get("coverage") or {}
        if cov.get("partial"):
            filled, total = cov.get("archive_windows_filled"), cov.get("archive_windows_total")
            archive_short = filled is not None and bool(total) and filled < total
            if cov.get("cells_filled", 0) < cov.get("cells_total", 0) or not archive_short:
                text += "\n" + self.tr("Part of the area could not be served; ask for a "
                                       "smaller area for the full result.")
            if archive_short:
                text += "\n" + self.tr("Only {0} of {1} archive windows (grid cell × month) "
                                       "were fetched; search again to fetch the rest."
                                       ).format(filled, total)
        self.lbl_summary.setText(text)
        self.lbl_temp.setVisible(True)
        self._set_status(self.tr("Results ready."))
        self.show_page("results")
        self._zoom_to_area()

    def _forget_layer(self) -> None:
        self.layer = None
        self.fids = {}

    def _zoom_to(self, bb: QgsRectangle) -> None:
        xf = QgsCoordinateTransform(WGS84, self.canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        self.canvas.setExtent(xf.transformBoundingBox(bb))
        self.canvas.refresh()

    def _zoom_to_area(self) -> None:
        """The study area with a proportional margin; wide swaths do not pull the view away."""
        bb = self.target_geom.boundingBox()
        pad = 0.3 if self.target["type"] == "point" else max(0.02, 0.25 * max(bb.width(),
                                                                            bb.height()))
        self._zoom_to(QgsRectangle(bb.xMinimum() - pad, bb.yMinimum() - pad,
                                   bb.xMaximum() + pad, bb.yMaximum() + pad))

    def _row_ids(self, item: QTreeWidgetItem) -> list:
        kind, ref = item.data(0, USER_ROLE) or (None, None)
        if kind == "row":
            return [ref]
        if kind == "pass":
            return list(self.passes[ref]["row_ids"])
        if kind == "day":
            return [rid for pid in ref for rid in self.passes[pid]["row_ids"]]
        return []

    def _select(self, item: QTreeWidgetItem) -> list:
        """Select the item's footprints on the layer; return their geometries."""
        if self.layer is None:
            return []
        fids = [self.fids[r] for r in self._row_ids(item) if r in self.fids]
        self.layer.selectByIds(fids)
        geoms = [self.layer.getFeature(f).geometry() for f in fids]
        return [g for g in geoms if not g.isEmpty()]

    def _on_result_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        geoms = self._select(item)
        if geoms:
            # one soft pulse to show where it is; QGIS's default is three solid-red flashes
            self.canvas.flashGeometries(geoms, WGS84, FLASH_START, FLASH_END, 1, FLASH_MS)

    def _on_result_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        geoms = self._select(item)
        if geoms:
            bb = QgsGeometry.unaryUnion(geoms).boundingBox()
            bb.combineExtentWith(self.target_geom.boundingBox())
            self._zoom_to(bb)

    # --- web links -----------------------------------------------------------

    def open_link(self) -> None:
        """Run the search of a swathview web permalink, as the web app does on opening it."""
        try:
            req = parse_web_link(self.edit_link.text())
            self._apply_request(req)
        except ValueError as exc:
            self._set_status(self._link_problem(str(exc)), True)
            return
        self._link_sats = list(req["satellites"])
        self.show_page("search")
        if self.missions:
            self._run_link_search()
        else:  # the list is still loading: search as soon as it arrives
            self._run_after_load = True
            self._set_status(self.tr("Search loaded from the link; it starts once the "
                                     "satellite list has loaded."))

    def _run_link_search(self) -> None:
        known = {m["key"] for m in self.missions}
        unknown = [k for k in self._link_sats if k not in known]
        if unknown:
            self.iface.messageBar().pushWarning(
                "SwathCast", self.tr("Not on this server: {0}.").format(", ".join(unknown)))
        self.run()

    def _link_problem(self, code: str) -> str:
        return {
            "no_query": self.tr("This link has no search in it. Copy the address from the web "
                                "app after running a search."),
            "no_satellites": self.tr("The link has no satellites (sats=)."),
            "bad_date": self.tr("The link has an invalid date (start= or end=)."),
        }.get(code, self.tr("The link could not be read ({0}).").format(code))

    def _apply_request(self, req: dict) -> None:
        """Put a request (from a link) into the form: area, satellites, dates, options."""
        t = req["target"]
        if t["type"] == "point":
            self._set_point(QgsPointXY(t["lon"], t["lat"]))
            self.area_mode.setCurrentIndex(AREA_POINT)
        elif t["type"] == "bbox":
            self._set_rect(QgsRectangle(t["west"], t["south"], t["east"], t["north"]),
                           self.tr("rectangle"))
            self.area_mode.setCurrentIndex(AREA_RECT)
        else:
            if t["type"] == "wkt":
                geom = QgsGeometry.fromWkt(t["wkt"])
            else:
                geom = QgsGeometry.fromWkt(ogr.CreateGeometryFromJson(
                    json.dumps(t["geometry"])).ExportToWkt()) if t["geometry"] else QgsGeometry()
            if geom.isNull() or geom.isEmpty() or \
                    geom.type() != compat.GEOMETRY_POLYGON:
                raise ValueError("bad_g")
            bb = geom.boundingBox()
            loc = QLocale()
            self._set_target(geojson_target(json.loads(geom.asJson(6))), geom,
                             self.tr("Area from the link: {0}° × {1}°").format(
                                 loc.toString(bb.width(), "f", 2),
                                 loc.toString(bb.height(), "f", 2)))
        if self.missions:
            self._check_missions(set(req["satellites"]))
        else:
            self._pending_sats = list(req["satellites"])
        start = QDate.fromString(req["start"], "yyyy-MM-dd")
        self.date_start.setDate(start)
        # the link's end is exclusive, like the API; "To" is inclusive
        self.date_end.setDate(QDate.fromString(req["end"], "yyyy-MM-dd").addDays(-1))
        opts = req["options"]
        self.chk_project.setChecked(opts["project"])
        self.spin_cov.setValue(round(opts["min_coverage"] * 100))

    def share_link(self) -> Optional[str]:
        """The web permalink of the search behind the results; polygons simplified to fit."""
        if self.last_req is None:
            return None
        req = dict(self.last_req)
        link = web_link(req)
        if len(link) > SHARE_MAX_CHARS and req["target"]["type"] == "geojson" \
                and self.target_geom is not None:
            for tolerance in (0.001, 0.005, 0.01, 0.05):
                geom = self.target_geom.simplify(tolerance)
                req["target"] = geojson_target(json.loads(geom.asJson(5)))
                link = web_link(req)
                if len(link) <= SHARE_MAX_CHARS:
                    break
        return link

    def share(self, open_browser: bool = False) -> None:
        try:
            link = self.share_link()
        except ValueError:
            link = None
        if not link:
            self._set_status(self.tr("This area cannot be shared as a web link."), True)
            return
        if open_browser:
            QDesktopServices.openUrl(QUrl(link))
            return
        QApplication.clipboard().setText(link)
        note = self.tr("Web link copied: paste it into an e-mail or a chat.")
        if len(link) > SHARE_MAX_CHARS:
            note += " " + self.tr("The area is detailed, so the link is long; some apps may "
                                  "cut it.")
        self.iface.messageBar().pushSuccess("SwathCast", note)
        self._set_status(note)

    # --- export --------------------------------------------------------------

    def export(self, fmt: str) -> None:
        if not self.job_id or self._download is not None:
            return
        ext, kind = {"ical": ("ics", self.tr("Calendar")), "kml": ("kml", "KML"),
                     "csv": ("csv", "CSV")}[fmt]
        last_dir = self.settings.value(SETTINGS + "export_dir", str(Path.home()))
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save {0}").format(kind),
            str(Path(last_dir) / f"swathcast_overpasses.{ext}"), f"{kind} (*.{ext})")
        if not path:
            return
        self.settings.setValue(SETTINGS + "export_dir", str(Path(path).parent))
        client, job_id = self._client(), self.job_id

        def work(_task):
            Path(path).write_bytes(client.download(job_id, fmt))
            return path

        def done(exception, value=None):
            self._download = None
            if exception is not None:
                self._set_status(self._error_text(exception), True)
            else:
                self.iface.messageBar().pushSuccess("SwathCast",
                                                    self.tr("Saved {0}").format(value))

        task = QgsTask.fromFunction(self.tr("Saving SwathCast export"), work, on_finished=done)
        self._download = task
        QgsApplication.taskManager().addTask(task)

    # --- lifecycle -----------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.rb_area.reset(compat.GEOMETRY_POLYGON)
        self.rb_point.reset(compat.GEOMETRY_POINT)
        if self.canvas.mapTool() in (self.tool_point, self.tool_rect):
            self._restore_tool()
        super().closeEvent(event)

    def unload(self) -> None:
        self._alive = False
        self.cancel()
        if self.canvas.mapTool() in (self.tool_point, self.tool_rect):
            self.canvas.unsetMapTool(self.canvas.mapTool())
        for rb in (self.rb_area, self.rb_point):
            rb.reset()
            self.canvas.scene().removeItem(rb)
