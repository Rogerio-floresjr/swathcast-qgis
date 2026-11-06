"""The dock end to end, offscreen, against a fake server built from real API replies.

Set SWATHCAST_LIVE=1 to also run the same flow against the public server.
"""
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication, QgsProject, QgsRectangle  # noqa: E402

DATA = Path(__file__).parent / "data"


class FakeTransport:
    """Answers like the swathview API, from fixtures captured from the live server."""

    calls: list = []
    coverage: dict = {}  # replaces result["coverage"] when set

    def __init__(self, api_key="", feedback=None):
        self.api_key = api_key

    def request(self, method, url, body, timeout_s):
        FakeTransport.calls.append((method, url))
        path = url.split("/api", 1)[1]
        if path == "/health":
            return 200, (DATA / "health.json").read_bytes()
        if path == "/missions":
            return 200, (DATA / "missions.json").read_bytes()
        if path == "/predict":
            return 200, b'{"job_id": "fake", "status": "running"}'
        if path == "/jobs/fake":
            result = json.loads((DATA / "paraibuna_result.json").read_text())
            if FakeTransport.coverage:
                result["coverage"] = FakeTransport.coverage
            return 200, json.dumps({"job_id": "fake", "status": "done", "result": result}).encode()
        if path == "/jobs/fake/export/ical":
            return 200, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        return 404, b'{"detail": "not found"}'


def dock_mod():
    from swathcast_qgis import dock

    return dock


def wait_for(cond, timeout=60.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise TimeoutError
        QgsApplication.processEvents()
        time.sleep(0.02)


@pytest.fixture
def dock(qgs_app, monkeypatch, request):
    from qgis.testing.mocked import get_iface

    from swathcast_qgis import dock as dock_mod

    if not getattr(request, "param", False):
        monkeypatch.setattr(dock_mod, "QgisTransport", FakeTransport)
    monkeypatch.setattr(dock_mod.QgsSettings, "value",
                        lambda self, key, default=None, *a, **k: default)
    monkeypatch.setattr(dock_mod.QgsSettings, "setValue", lambda self, *a, **k: None)
    QgsProject.instance().clear()
    d = dock_mod.SwathcastDock(get_iface())
    wait_for(lambda: d._loader is None, timeout=180)
    yield d
    d.unload()


def paraibuna(d):
    d._set_rect(QgsRectangle(-46.0, -23.65, -45.45, -23.25), "rectangle")
    from qgis.PyQt.QtCore import QDate

    d.date_start.setDate(QDate(2026, 10, 20))
    d.date_end.setDate(QDate(2026, 11, 2))


def test_missions_listed_with_default_selection(dock):
    assert dock.btn_run.isEnabled(), dock.lbl_status.text()
    assert len(list(dock._mission_items())) == 19
    assert set(dock.checked_missions()) == {"sentinel-2", "landsat-8", "landsat-9", "cbers-4",
                                            "cbers-4a", "amazonia-1"}
    groups = [dock.tree_sat.topLevelItem(i).text(0) for i in range(dock.tree_sat.topLevelItemCount())]
    assert groups[0] == "INPE"
    dock._check_missions(None)
    assert len(dock.checked_missions()) == 19


def test_run_lists_passes_and_keeps_records(dock):
    paraibuna(dock)
    assert dock.target["type"] == "bbox"
    dock.run()
    wait_for(lambda: dock._running is None)
    assert dock.layer is not None, dock.lbl_status.text()
    rows = json.loads((DATA / "paraibuna_result.json").read_text())["overpasses"]
    assert dock.layer.featureCount() == len(rows)
    assert dock.layer.temporalProperties().isActive()
    # 18 records (S2 tiles, Landsat/CBERS scenes) are 9 passes
    assert len(dock.passes) == 9
    listed = sum(dock.tree_res.topLevelItem(i).childCount()
                 for i in range(dock.tree_res.topLevelItemCount()))
    assert listed == len(dock.passes)
    assert sorted(i for p in dock.passes for i in p["row_ids"]) == sorted(r["id"] for r in rows)
    assert dock.lbl_summary.text().startswith("9 passes · 18 records · ")
    group = QgsProject.instance().layerTreeRoot().children()[0]
    assert group.name().startswith("SwathCast · ")
    # every pass has a union coverage in [0, 1], never more than the area itself
    assert all(p["coverage"] is not None and 0 <= p["coverage"] <= 1 for p in dock.passes)
    # clicking a pass selects all of its records
    first = dock.tree_res.topLevelItem(0).child(0)
    kind, pid = first.data(0, dock_mod().USER_ROLE)
    assert kind == "pass"
    dock._on_result_clicked(first, 0)
    assert dock.layer.selectedFeatureCount() == len(dock.passes[pid]["row_ids"])
    assert all(b.isEnabled() for b in dock.export_buttons)
    # the temporary drawing gave way to the area layer
    assert dock.rb_area.numberOfVertices() == 0
    assert dock.lbl_temp.isVisible() or not dock.isVisible()


def test_union_coverage_is_not_a_sum_of_tiles(dock):
    paraibuna(dock)
    dock.run()
    wait_for(lambda: dock._running is None)
    rows = {r["id"]: r for r in json.loads((DATA / "paraibuna_result.json").read_text())["overpasses"]}
    for p in dock.passes:
        tiles = [rows[i]["coverage_fraction"] for i in p["row_ids"]]
        assert p["coverage"] <= sum(tiles) + 1e-3
        assert p["coverage"] >= max(tiles) - 1e-3


def test_point_area_reads_point_inside(dock):
    from qgis.core import QgsPointXY

    dock._set_point(QgsPointXY(-45.68, -23.42))
    assert dock._coverage_text(1.0) == "point inside"


def test_coverage_text(dock):
    paraibuna(dock)
    assert dock._coverage_text(0.004) == "<1 %"
    assert dock._coverage_text(0.0) == "edge only"
    assert dock._coverage_text(None) == "—"
    assert dock._coverage_text(0.426) == "43 %"


def test_partial_archive_says_how_much_was_fetched(dock, monkeypatch):
    monkeypatch.setattr(FakeTransport, "coverage", {
        "cells_total": 1, "cells_filled": 1, "partial": True,
        "archive_windows_total": 30, "archive_windows_filled": 12})
    paraibuna(dock)
    dock.run()
    wait_for(lambda: dock._running is None)
    summary = dock.lbl_summary.text()
    assert "Only 12 of 30 archive windows" in summary
    assert "Part of the area" not in summary  # the forecast itself was complete


def test_end_date_is_inclusive(dock, monkeypatch):
    sent = {}
    from swathcast_qgis import client as client_mod

    real = client_mod.SwathviewClient.run

    def spy(self, req, *a, **k):
        sent.update(req)
        return real(self, req, *a, **k)

    monkeypatch.setattr(client_mod.SwathviewClient, "run", spy)
    paraibuna(dock)
    dock.run()
    wait_for(lambda: dock._running is None)
    assert (sent["start"], sent["end"]) == ("2026-10-20", "2026-11-03")


def test_limits_checked_before_sending(dock):
    paraibuna(dock)
    dock.date_end.setDate(dock.date_start.date().addDays(120))
    FakeTransport.calls.clear()
    dock.run()
    assert dock._running is None
    assert "120" in dock.lbl_status.text() or "121" in dock.lbl_status.text()
    assert not any(m == "POST" for m, _ in FakeTransport.calls)


def test_no_area_is_explained(dock):
    dock.run()
    assert dock._running is None and dock.lbl_status.text()


def test_export_calendar(dock, monkeypatch, tmp_path):
    from swathcast_qgis import dock as dock_mod

    paraibuna(dock)
    dock.run()
    wait_for(lambda: dock._running is None)
    out = tmp_path / "agenda.ics"
    monkeypatch.setattr(dock_mod.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    dock.export("ical")
    wait_for(lambda: dock._download is None)
    assert out.read_bytes().startswith(b"BEGIN:VCALENDAR")


@pytest.mark.skipif(os.environ.get("SWATHCAST_LIVE") != "1", reason="set SWATHCAST_LIVE=1")
@pytest.mark.parametrize("dock", [True], indirect=True)
def test_live_server(dock):
    paraibuna(dock)
    dock.run()
    wait_for(lambda: dock._running is None, timeout=300)
    assert dock.layer is not None and dock.layer.featureCount() > 0, dock.lbl_status.text()


def test_tabs_and_pinned_search_button(dock):
    assert list(dock.rail_buttons) == ["search", "results", "advanced", "info"]
    assert all(b.accessibleName() for b in dock.rail_buttons.values())
    # Find belongs to the Search tab only, pinned under its scrolling form
    search = dock.stack.widget(0)
    assert search.isAncestorOf(dock.btn_run)
    assert not search.findChild(dock_mod().QScrollArea).widget().isAncestorOf(dock.btn_run)
    for i in range(1, dock.stack.count()):
        assert not dock.stack.widget(i).isAncestorOf(dock.btn_run)


def test_results_open_the_results_tab_and_share(dock, monkeypatch):
    from qgis.PyQt.QtWidgets import QApplication

    paraibuna(dock)
    assert not dock.btn_share.isEnabled()
    dock.run()
    wait_for(lambda: dock._running is None)
    assert dock.stack.currentIndex() == 1
    dock.share()
    link = QApplication.clipboard().text()
    assert link.startswith("https://swathview.pages.dev/?t=bbox&bbox=-46,-23.65,-45.45,-23.25")
    assert "start=2026-10-20&end=2026-11-03" in link  # 'To' 2 Nov inclusive → end 3 Nov


def test_open_link_fills_the_form(dock):
    from qgis.PyQt.QtCore import QDate

    dock.edit_link.setText("https://swathview.pages.dev/?t=point&lat=-23.42&lon=-45.68"
                           "&sats=sentinel-2,cbers-4&start=2026-10-21&end=2026-11-11"
                           "&archive=1&project=0&mincov=0.3")
    dock.open_link()
    assert dock.target == {"type": "point", "lat": -23.42, "lon": -45.68}
    assert set(dock.checked_missions()) == {"sentinel-2", "cbers-4"}
    assert dock.date_start.date() == QDate(2026, 10, 21)
    assert dock.date_end.date() == QDate(2026, 11, 10)  # exclusive end → inclusive 'To'
    assert not dock.chk_project.isChecked()  # archive=1 is an old flag, read and ignored
    assert dock.spin_cov.value() == 30
    # like the web app, opening the link runs the search
    assert dock._running is not None
    wait_for(lambda: dock._running is None)
    assert dock.stack.currentIndex() == 1 and dock.layer is not None
    # a polygon link, in WKT, becomes a GeoJSON area
    dock.edit_link.setText("t=geometry&g=POLYGON((-46 -23,-45 -23,-45 -22,-46 -23))"
                           "&sats=landsat-9")
    dock.open_link()
    assert dock.target["type"] == "geojson"
    wait_for(lambda: dock._running is None)
    dock.edit_link.setText("https://swathview.pages.dev/")
    dock.open_link()
    assert "no search" in dock.lbl_status.text()


def test_fits_a_narrow_dock(dock):
    """A normal search fits 360 logical px: the dock's minimum width, in both languages.

    The font is pinned to 10 pt, a little above QGIS's default (Segoe UI 9 pt on
    Windows): offscreen Qt5 would otherwise use 12 pt and Qt6 9 pt.
    """
    from qgis.core import QgsApplication
    from qgis.PyQt.QtGui import QFont

    from swathcast_qgis import dock as dock_mod
    from swathcast_qgis.i18n import install_translator

    old = QgsApplication.font()
    QgsApplication.setFont(QFont("DejaVu Sans", 10))
    pt = install_translator("pt_BR")
    try:
        for lang in ("pt_BR", "en"):
            if lang == "en":
                QgsApplication.removeTranslator(pt)
            d = dock_mod.SwathcastDock(dock.iface)
            wait_for(lambda: d._loader is None)
            d.show()  # measure styled widgets: layouts size unpolished ones by Qt's defaults
            for key in d.rail_buttons:
                d.show_page(key)
                QgsApplication.processEvents()
                assert d.minimumSizeHint().width() <= 360, (lang, key)
            d.hide()
            d.unload()
    finally:
        QgsApplication.removeTranslator(pt)
        QgsApplication.setFont(old)


def test_link_opened_while_loading_runs_after_the_list(qgs_app, monkeypatch):
    from qgis.testing.mocked import get_iface

    from swathcast_qgis import dock as dock_mod

    monkeypatch.setattr(dock_mod, "QgisTransport", FakeTransport)
    monkeypatch.setattr(dock_mod.QgsSettings, "value",
                        lambda self, key, default=None, *a, **k: default)
    monkeypatch.setattr(dock_mod.QgsSettings, "setValue", lambda self, *a, **k: None)
    d = dock_mod.SwathcastDock(get_iface())
    # until the list arrives the box shows a spinner, not an empty tree
    assert d.sat_stack.currentIndex() == 0 and not d.sat_spinner.isHidden()
    d.edit_link.setText("t=point&lat=-23.42&lon=-45.68&sats=sentinel-2")
    d.open_link()
    assert d._run_after_load and d._running is None
    wait_for(lambda: d._loader is None)
    assert d.sat_stack.currentIndex() == 1
    assert d._running is not None  # started on its own once the list loaded
    wait_for(lambda: d._running is None)
    d.unload()


def test_quick_picks_share_a_line_when_they_fit(dock):
    from qgis.PyQt.QtWidgets import QPushButton

    chips = [b for b in dock.tree_sat.parentWidget().parentWidget().findChildren(QPushButton)
             if b.objectName() == "chip"]
    flow = chips[0].parentWidget().layout()
    one_line = sum(b.sizeHint().width() for b in chips) + flow.spacing() * (len(chips) - 1)
    line = chips[0].sizeHint().height()
    assert flow.heightForWidth(one_line) == line
    assert flow.heightForWidth(one_line - 10) > line  # narrower: wraps


def test_network_errors_are_classified(qgs_app):
    from qgis.PyQt.QtNetwork import QNetworkReply

    from swathcast_qgis import client
    from swathcast_qgis.transport import network_status

    E = QNetworkReply.NetworkError
    assert network_status(E.ProxyConnectionRefusedError) == client.NET_PROXY
    assert network_status(E.HostNotFoundError) == client.NET_FATAL
    for transient in (E.TimeoutError, E.ConnectionRefusedError, E.RemoteHostClosedError):
        assert network_status(transient) == client.NET_TRANSIENT


def test_proxy_refusal_says_so(dock):
    from swathcast_qgis.client import NET_PROXY, ApiError

    text = dock._error_text(ApiError("Connection to proxy refused", NET_PROXY))
    assert "proxy" in text and "Options" in text


def test_remembered_satellite_list_shows_before_the_server_answers(qgs_app, monkeypatch):
    from qgis.testing.mocked import get_iface

    from swathcast_qgis import dock as dock_mod

    store = {}
    monkeypatch.setattr(dock_mod, "QgisTransport", FakeTransport)
    monkeypatch.setattr(dock_mod.QgsSettings, "value",
                        lambda self, key, default=None, *a, **k: store.get(key, default))
    monkeypatch.setattr(dock_mod.QgsSettings, "setValue",
                        lambda self, key, value, *a, **k: store.__setitem__(key, value))
    d = dock_mod.SwathcastDock(get_iface())
    wait_for(lambda: d._loader is None)
    assert "swathcast_qgis/missions_cache" in store
    d.unload()
    # next opening: the list is there at once and searchable while the server wakes
    d = dock_mod.SwathcastDock(get_iface())
    assert d._loader is not None
    assert d.sat_stack.currentIndex() == 1 and len(list(d._mission_items())) == 19
    assert d.btn_run.isEnabled()
    d._check_missions({"terra"})
    wait_for(lambda: d._loader is None)
    assert d.checked_missions() == ["terra"]  # the refresh kept the user's ticks
    d.unload()
