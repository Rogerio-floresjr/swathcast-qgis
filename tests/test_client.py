"""Pure-Python tests of the API client (no QGIS needed)."""
import json
from datetime import date
from pathlib import Path

import pytest

from swathcast_qgis import client as c


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, body, timeout_s):
        self.calls.append((method, url, json.loads(body) if body else None))
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload).encode() if payload is not None else b""


def run(transport, req=None):
    sleeps = []
    cl = c.SwathviewClient(transport, "https://example.org/")
    out = cl.run(req or {"x": 1}, on_status=lambda s: None, is_canceled=lambda: False,
                 sleep=sleeps.append, clock=lambda: 0.0)
    return out, sleeps


def test_run_wakes_submits_and_polls():
    t = FakeTransport([
        (503, None), (200, {"status": "ok"}),
        (200, {"job_id": "abc", "status": "running"}),
        (200, {"job_id": "abc", "status": "running"}),
        (200, {"job_id": "abc", "status": "done", "result": {"overpasses": []}}),
    ])
    (job_id, result), sleeps = run(t)
    assert job_id == "abc" and result == {"overpasses": []}
    assert [m for m, *_ in t.calls] == ["GET", "GET", "POST", "GET", "GET"]
    assert t.calls[2][1] == "https://example.org/api/predict"
    assert sleeps == [5, 0.5]


def test_server_refusal_carries_detail():
    t = FakeTransport([(200, {"status": "ok"}),
                       (422, {"detail": "time window is 120 days; this server allows at most 90"})])
    with pytest.raises(c.ApiError, match="120 days") as exc:
        run(t)
    assert exc.value.status == 422


def test_job_error_is_raised():
    t = FakeTransport([(200, {}), (200, {"job_id": "j"}),
                       (200, {"status": "error", "error": "boom"})])
    with pytest.raises(c.ApiError, match="boom"):
        run(t)


def test_cancel_stops_polling():
    t = FakeTransport([(200, {}), (200, {"job_id": "j"})])
    cl = c.SwathviewClient(t)
    calls = iter([False, True])
    with pytest.raises(c.Canceled):
        cl.run({}, lambda s: None, lambda: next(calls), sleep=lambda s: None)


def test_limits():
    req = c.build_request(c.bbox_target(-50, -20, -35, -5), ["sentinel-2", "prisma"],
                          date(2026, 10, 1), date(2027, 1, 30))
    limits = {"max_window_days": 90, "max_aoi_deg2": 100.0, "live_max_aoi_deg2": 1.0}
    codes = [code for code, _ in c.limit_problems(req, limits, {"prisma", "enmap"})]
    assert codes == ["window_too_long", "aoi_too_big", "tasking_aoi_too_big"]
    small = c.build_request(c.point_target(-23.4, -45.7), ["prisma"],
                            date(2026, 10, 1), date(2026, 10, 20))
    assert c.limit_problems(small, limits, {"prisma"}) == []
    assert c.limit_problems(small, {}, set()) == []


def test_geojson_bounds():
    g = {"type": "MultiPolygon", "coordinates": [[[[-46, -23.6], [-45.4, -23.6],
                                                   [-45.4, -23.2], [-46, -23.6]]]]}
    assert c.target_bounds(c.geojson_target(g)) == (-46, -23.6, -45.4, -23.2)
    assert c.target_area_deg2(c.point_target(-23, -45)) == 0


def test_join_and_wkt():
    result = {
        "overpasses": [{"id": 0, "source": "plan"}, {"id": 1, "source": "repeat"},
                       {"id": 2, "source": "orbit", "opportunity_only": True}],
        "footprints": {"features": [
            {"id": 0, "geometry": {"type": "Polygon",
                                   "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
            {"id": 1, "geometry": None},
        ]},
    }
    rows = c.join_rows(result)
    assert [g is not None for _, g in rows] == [True, False, False]
    assert c.geometry_to_multipolygon_wkt(rows[0][1]) == "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))"
    assert [c.evidence(r) for r, _ in rows] == ["planned", "cycle_projected", "orbit_derived"]
    assert [c.tasking_required(r) for r, _ in rows] == [False, False, True]


def test_evidence_covers_all_sources():
    for source in ("plan", "landsat-cal", "inpe-grid", "orbit", "repeat", "archive",
                   "asi-prefeas", "satseer"):
        assert c.EVIDENCE_BY_SOURCE[source] in c.EVIDENCE_ORDER
    assert c.evidence({"source": "something-new"}) == "unspecified"
    assert c.evidence({"source": "inpe-grid"}) == "archive_derived"


def test_evidence_prefers_server_code():
    assert c.evidence({"source": "orbit", "evidence": "operator_forecast"}) == "operator_forecast"
    # an unknown server code falls back to the local mapping
    assert c.evidence({"source": "plan", "evidence": "brand_new"}) == "planned"
    assert c.evidence({"source": "plan", "evidence": None}) == "planned"


def test_mission_groups():
    assert c.mission_group("cbers-4a-wpm") == "INPE"
    assert c.mission_group("noaa-21") == "VIIRS"
    assert c.mission_group("aqua") == "MODIS"


# --- passes ------------------------------------------------------------------

DATA = Path(__file__).parent / "data"


def rec(i, ts, sat="Sentinel-3A", source="orbit", **kw):
    return {"id": i, "timestamp_utc": ts, "date": ts[:10], "mission": "m", "satellite": sat,
            "sensor": "S", "source": source, **kw}


def test_sentinel2_tiles_of_one_datatake_are_one_pass():
    rows = json.loads((DATA / "probe_bbox_result.json").read_text())["overpasses"]
    passes = c.group_passes(rows)
    s2 = [p for p in passes if p["mission"] == "sentinel-2" and p["source"] == "plan"]
    assert len(s2) == 5 and all(len(p["row_ids"]) >= 10 for p in s2)
    assert sorted(i for p in passes for i in p["row_ids"]) == sorted(r["id"] for r in rows)


def test_landsat_scenes_of_one_path_and_day_are_one_pass():
    rows = json.loads((DATA / "probe_bbox_result.json").read_text())["overpasses"]
    l9 = [p for p in c.group_passes(rows) if p["mission"] == "landsat-9"]
    assert [len(p["row_ids"]) for p in l9] == [3, 3, 2]
    assert l9[0]["grid_cells"] == ["218/075", "218/076", "218/077"]


def test_two_passes_on_the_same_day_stay_apart():
    rows = [rec(0, "2026-10-20T12:00:00+00:00"), rec(1, "2026-10-20T12:01:00+00:00"),
            rec(2, "2026-10-20T13:41:00+00:00")]
    passes = c.group_passes(rows)
    assert [p["row_ids"] for p in passes] == [[0, 1], [2]]
    assert passes[0]["grouped_by_time"] and not passes[1]["grouped_by_time"]


def test_projected_cycles_stay_apart():
    rows = [rec(0, "2026-10-27T13:18:00+00:00", "Sentinel-2B", "repeat", orbit_relative=138),
            rec(1, "2026-11-06T13:18:00+00:00", "Sentinel-2B", "repeat", orbit_relative=138)]
    assert len(c.group_passes(rows)) == 2


def test_plan_and_catalogue_product_are_not_merged():
    rows = [rec(0, "2026-10-01T13:18:00+00:00", "Sentinel-2C", "plan", datatake_id="9-1"),
            rec(1, "2026-10-01T13:18:00+00:00", "Sentinel-2C", "archive", datatake_id="9-1")]
    assert len(c.group_passes(rows)) == 2


def test_pass_summary_fields():
    rows = [rec(0, "2026-10-01T13:18:00+00:00", source="archive", datatake_id="7",
                grid_cell="23KMQ", cloud_cover=40.0, coverage_fraction=0.2),
            rec(1, "2026-10-01T13:18:20+00:00", source="archive", datatake_id="7",
                grid_cell="23KMR", cloud_cover=10.0, coverage_fraction=0.5)]
    (p,) = c.group_passes(rows)
    assert p["evidence"] == "in_catalogue"
    assert p["cloud_range"] == (10.0, 40.0) and p["max_record_coverage"] == 0.5
    assert (p["start"], p["end"]) == (rows[0]["timestamp_utc"], rows[1]["timestamp_utc"])


WEB = ("https://swathview.pages.dev/?t=point&lat=-23.42&lon=-45.68&sats=sentinel-2"
       "&start=2026-10-21&end=2026-11-11")


def test_web_link_round_trips_the_web_apps_own_link():
    req = c.parse_web_link(WEB)
    assert req["target"] == {"type": "point", "lat": -23.42, "lon": -45.68}
    assert (req["start"], req["end"]) == ("2026-10-21", "2026-11-11")  # end stays exclusive
    assert req["options"] == {"project": True, "min_coverage": 0.0}
    assert c.web_link(req) == WEB


def test_web_link_every_field():
    req = c.build_request(c.bbox_target(-46, -23.65, -45.45, -23.25),
                          ["sentinel-2", "cbers-4a"], date(2026, 1, 1), date(2026, 1, 15),
                          project=False, min_coverage=0.25)
    link = c.web_link(req)
    assert "bbox=-46,-23.65,-45.45,-23.25" in link and "sats=sentinel-2,cbers-4a" in link
    assert c.parse_web_link(link) == req
    # the query string alone, as copied from the address bar, works too
    assert c.parse_web_link("?" + link.split("?", 1)[1]) == req


def test_web_link_polygon_as_wkt_and_geojson():
    poly = {"type": "Polygon", "coordinates": [[[-46, -23], [-45, -23], [-45, -22], [-46, -23]]]}
    req = c.build_request(c.geojson_target(poly), ["landsat-9"], date(2026, 1, 1),
                          date(2026, 1, 8))
    back = c.parse_web_link(c.web_link(req))
    assert back["target"] == {"type": "wkt",
                              "wkt": "MULTIPOLYGON (((-46 -23, -45 -23, -45 -22, -46 -23)))"}
    gj = c.parse_web_link("t=geometry&g=" + json.dumps(poly) + "&sats=landsat-9")
    assert gj["target"] == {"type": "geojson", "geometry": poly}


def test_web_link_defaults_and_errors():
    req = c.parse_web_link("t=point&lat=1&lon=2&sats=modis-terra", today=date(2026, 3, 1))
    assert (req["start"], req["end"]) == ("2026-03-01", "2026-03-22")  # the web app's 21 days
    for bad, code in (("https://swathview.pages.dev/", "no_query"),
                      ("t=point&lat=95&lon=0&sats=x", "bad_lat"),
                      ("t=bbox&bbox=1,2,3&sats=x", "bad_bbox"),
                      ("t=geometry&g=&sats=x", "bad_g"),
                      ("t=circle&sats=x", "bad_t"),
                      ("t=point&lat=0&lon=0", "no_satellites"),
                      ("t=point&lat=0&lon=0&sats=x&start=21/10/2026", "bad_date"),
                      ("t=point&lat=0&lon=0&sats=x&mincov=50", "bad_mincov")):
        with pytest.raises(ValueError, match=code):
            c.parse_web_link(bad)


def test_wake_retries_a_silent_server_but_not_a_refusing_proxy():
    # no answer yet (a booting free server): wait and try again
    t = FakeTransport([(0, None), (200, {"status": "ok"})])
    sleeps = []
    cl = c.SwathviewClient(t, "https://example.org/")
    assert cl.wake(lambda s: None, lambda: False, sleep=sleeps.append) == {"status": "ok"}
    assert sleeps == [5]
    # QGIS's proxy refused, or DNS/SSL failed: retrying will not help, so fail at once
    for code in (c.NET_PROXY, c.NET_FATAL):
        t = FakeTransport([(code, None)])
        sleeps = []
        cl = c.SwathviewClient(t, "https://example.org/")
        with pytest.raises(c.ApiError) as err:
            cl.wake(lambda s: None, lambda: False, sleep=sleeps.append)
        assert err.value.status == code and sleeps == [] and len(t.calls) == 1


def test_old_links_with_archive_and_historical_still_open():
    # the server answers past dates from the catalogues now; both flags are ignored
    req = c.parse_web_link(WEB + "&archive=1&historical=1")
    assert req["options"] == {"project": True, "min_coverage": 0.0}
    assert c.web_link(req) == WEB
