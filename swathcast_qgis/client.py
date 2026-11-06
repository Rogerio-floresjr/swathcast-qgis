"""swathview API client: request building, limit checks, polling, result joining.

No QGIS imports here. The HTTP transport is injected, so this module is tested with
plain Python, and the plugin routes every call through QGIS's own network stack
(proxy, SSL and authentication settings the user already configured).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit
from typing import Any, Callable, Iterable, Iterator, Optional, Protocol

DEFAULT_BASE_URL = "https://swathview-api.onrender.com"
#: the web app whose permalinks the plugin reads and writes
WEB_URL = "https://swathview.pages.dev/"
WEB_DEFAULT_DAYS = 21  # the web app's window when a link carries no dates

#: transport status codes for replies without an HTTP status
NET_TRANSIENT = 0   # no answer yet (timeout, refused or closed): the server may be booting
NET_PROXY = -1      # QGIS's proxy refused or is missing: retrying will not help
NET_FATAL = -2      # DNS, SSL and the like: retrying will not help

# A free server sleeps when idle and takes up to about a minute to wake.
WAKE_TIMEOUT_S = 150.0
JOB_TIMEOUT_S = 600.0

# What kind of evidence each prediction source is, in the words the user sees.
# "Tasking required" (opportunity_only) is a separate flag shown next to it.
EVIDENCE_BY_SOURCE = {
    "archive": "in_catalogue",          # product found in a catalogue
    "plan": "planned",                  # ESA acquisition plan (Sentinel-1, -2)
    "landsat-cal": "planned",           # USGS acquisition calendar (Landsat)
    "asi-prefeas": "operator_forecast",  # the operator's own feasibility forecast (PRISMA)
    "inpe-grid": "archive_derived",     # INPE path/row schedule learned from its catalogue
    "repeat": "cycle_projected",        # last known repeat cycle carried forward
    "orbit": "orbit_derived",           # orbit propagation: the sensor can see the area
    "satseer": "orbit_derived",
}
EVIDENCE_ORDER = ("in_catalogue", "planned", "operator_forecast", "archive_derived",
                  "cycle_projected", "orbit_derived", "unspecified")

# Consecutive records of one satellite closer than half an orbit (LEO periods are
# 95-101 min) belong to the same pass; two passes of one satellite are ~100 min apart.
HALF_ORBIT_S = 45 * 60

# Picker groups, in the swathview order (INPE first).
GROUP_ORDER = ("INPE", "Sentinel", "Landsat", "MODIS", "VIIRS", "PACE",
               "Hyperspectral", "Other")


class ApiError(Exception):
    """A request the server refused, or a server that could not be reached."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class Canceled(Exception):
    """The user canceled the run."""


class Transport(Protocol):
    def request(self, method: str, url: str, body: Optional[bytes],
                timeout_s: float) -> tuple[int, bytes]:
        """Return (HTTP status, body). Status 0 means no HTTP response at all."""


# --- targets -----------------------------------------------------------------

def point_target(lat: float, lon: float) -> dict:
    return {"type": "point", "lat": round(lat, 6), "lon": round(lon, 6)}


def bbox_target(west: float, south: float, east: float, north: float) -> dict:
    return {"type": "bbox", "west": round(west, 6), "south": round(south, 6),
            "east": round(east, 6), "north": round(north, 6)}


def geojson_target(geometry: dict) -> dict:
    return {"type": "geojson", "geometry": geometry}


def _coords(obj: Any) -> Iterator[tuple[float, float]]:
    if isinstance(obj, (list, tuple)) and obj and isinstance(obj[0], (int, float)):
        yield float(obj[0]), float(obj[1])
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _coords(item)


def target_bounds(target: dict) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a target, in degrees."""
    kind = target["type"]
    if kind == "point":
        return target["lon"], target["lat"], target["lon"], target["lat"]
    if kind == "bbox":
        return target["west"], target["south"], target["east"], target["north"]
    if kind == "geojson":
        geom = target["geometry"]
        parts = geom.get("geometries", [geom])
        pts = [p for g in parts for p in _coords(g.get("coordinates", []))]
        if not pts:
            raise ValueError("empty geometry")
        xs, ys = zip(*pts)
        return min(xs), min(ys), max(xs), max(ys)
    raise ValueError(f"unknown target type: {kind}")


def target_area_deg2(target: dict) -> float:
    """Bounding-box area in square degrees, the measure the server limits."""
    w, s, e, n = target_bounds(target)
    return max(0.0, e - w) * max(0.0, n - s)


# --- request -----------------------------------------------------------------

def build_request(target: dict, satellites: Iterable[str], start: date, end: date,
                  *, project: bool = True, min_coverage: float = 0.0) -> dict:
    return {
        "target": target,
        "satellites": list(satellites),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "options": {"project": project, "min_coverage": min_coverage},
    }


# --- web app permalinks --------------------------------------------------------
# The web app keeps its form in the query string (frontend/src/state/urlState.ts):
# t=point&lat=&lon= | t=bbox&bbox=w,s,e,n | t=geometry&g=<GeoJSON or WKT>,
# sats=a,b  start=YYYY-MM-DD  end=YYYY-MM-DD (exclusive, like the API),
# project=0  mincov=<0..1>. Older links may carry archive=1 and historical=1: the
# server answers past dates from the catalogues now, so both are read and ignored.

def web_link(req: dict, base: str = WEB_URL) -> str:
    """The web app permalink of a request (the inverse of ``parse_web_link``)."""
    t, opts = req["target"], req.get("options") or {}
    q: list[tuple[str, str]] = []
    if t["type"] == "point":
        q += [("t", "point"), ("lat", f"{t['lat']:.6g}"), ("lon", f"{t['lon']:.6g}")]
    elif t["type"] == "bbox":
        q += [("t", "bbox"), ("bbox", ",".join(f"{t[k]:.6g}" for k in
                                               ("west", "south", "east", "north")))]
    else:
        # WKT is about half the length of GeoJSON; the web app takes either
        wkt = t["wkt"] if t["type"] == "wkt" else geometry_to_multipolygon_wkt(t["geometry"])
        if not wkt:
            raise ValueError("only polygon areas can be shared")
        q += [("t", "geometry"), ("g", wkt)]
    q += [("sats", ",".join(req["satellites"])), ("start", req["start"]), ("end", req["end"])]
    if not opts.get("project", True):
        q.append(("project", "0"))
    if opts.get("min_coverage"):
        q.append(("mincov", f"{opts['min_coverage']:g}"))
    return base + "?" + urlencode(q, safe=",")


def _number(params: dict, key: str, lo: float, hi: float) -> float:
    try:
        value = float(params[key])
    except (KeyError, ValueError):
        raise ValueError(f"bad_{key}") from None
    if not lo <= value <= hi:
        raise ValueError(f"bad_{key}")
    return value


def parse_web_link(text: str, today: Optional[date] = None) -> dict:
    """A web app permalink (or just its query string) → a request, as ``build_request``.

    Raises ValueError with a short code: ``no_query``, ``bad_t``, ``bad_lat``,
    ``bad_lon``, ``bad_bbox``, ``bad_g``, ``no_satellites``, ``bad_date``, ``bad_mincov``.
    """
    text = text.strip()
    query = urlsplit(text).query if "://" in text else text.lstrip("?")
    params = dict(parse_qsl(query, keep_blank_values=True))
    if not params:
        raise ValueError("no_query")
    kind = params.get("t", "point")
    if kind == "point":
        target = point_target(_number(params, "lat", -90, 90), _number(params, "lon", -180, 180))
    elif kind == "bbox":
        try:
            w, s, e, n = (float(v) for v in params.get("bbox", "").split(","))
        except ValueError:
            raise ValueError("bad_bbox") from None
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
            raise ValueError("bad_bbox")
        target = bbox_target(w, s, e, n)
    elif kind == "geometry":
        g = params.get("g", "").strip()
        if g.startswith("{"):
            try:
                geom = json.loads(g)
            except ValueError:
                raise ValueError("bad_g") from None
            if geom.get("type") == "Feature":
                geom = geom.get("geometry") or {}
            target = geojson_target(geom)
        elif g:
            target = {"type": "wkt", "wkt": g}
        else:
            raise ValueError("bad_g")
    else:
        raise ValueError("bad_t")
    sats = [k for k in params.get("sats", "").split(",") if k]
    if not sats:
        raise ValueError("no_satellites")
    today = today or date.today()
    try:
        start = date.fromisoformat(params["start"]) if params.get("start") else today
        end = (date.fromisoformat(params["end"]) if params.get("end")
               else today + timedelta(days=WEB_DEFAULT_DAYS))
    except ValueError:
        raise ValueError("bad_date") from None
    min_cov = _number(params, "mincov", 0, 1) if "mincov" in params else 0.0
    return build_request(target, sats, start, end,
                         project=params.get("project") != "0",
                         min_coverage=min_cov)


def limit_problems(req: dict, limits: dict, tasking: set[str]) -> list[tuple[str, dict]]:
    """Requests the server would refuse, found before sending.

    Returns (code, params) pairs; the dock turns each into a translated message.
    ``limits`` is the ``limits`` object of /api/health (empty when unlimited).
    """
    out: list[tuple[str, dict]] = []
    if not req["satellites"]:
        out.append(("no_satellites", {}))
    start, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
    days = (end - start).days
    if days <= 0:
        out.append(("bad_window", {}))
    max_days = limits.get("max_window_days")
    if max_days and days > max_days:
        out.append(("window_too_long", {"days": days, "max": max_days}))
    area = target_area_deg2(req["target"])
    max_area = limits.get("max_aoi_deg2")
    if max_area and area > max_area:
        out.append(("aoi_too_big", {"area": area, "max": max_area}))
    live_max = limits.get("live_max_aoi_deg2")
    live = sorted(s for s in req["satellites"] if s in tasking)
    if live_max and live and area > live_max:
        out.append(("tasking_aoi_too_big",
                    {"area": area, "max": live_max, "missions": ", ".join(live)}))
    return out


# --- client ------------------------------------------------------------------

def poll_delays() -> Iterator[float]:
    """Seconds to wait between job polls: quick at first, then every 5 s."""
    yield from (0.5, 1, 1, 2, 2, 3, 3)
    while True:
        yield 5


class SwathviewClient:
    def __init__(self, transport: Transport, base_url: str = DEFAULT_BASE_URL) -> None:
        self.transport = transport
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _raw(self, method: str, path: str, payload: Optional[dict] = None,
             timeout_s: float = 60.0) -> bytes:
        body = json.dumps(payload).encode() if payload is not None else None
        status, content = self.transport.request(method, self.base_url + path, body, timeout_s)
        if status <= 0:
            raise ApiError(content.decode(errors="replace") or "server unreachable", status)
        if status >= 400:
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, list):  # FastAPI validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise ApiError(str(detail or f"HTTP {status}"), status)
        return content

    def _call(self, method: str, path: str, payload: Optional[dict] = None,
              timeout_s: float = 60.0) -> Any:
        content = self._raw(method, path, payload, timeout_s)
        return json.loads(content) if content else None

    def health(self) -> dict:
        return self._call("GET", "/api/health", timeout_s=90.0)

    def wake(self, on_status: Callable[[str], None], is_canceled: Callable[[], bool],
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> dict:
        """Health-check until the server answers; a sleeping free server can take a minute."""
        deadline = clock() + WAKE_TIMEOUT_S
        first = True
        while True:
            if is_canceled():
                raise Canceled()
            try:
                return self.health()
            except ApiError as exc:
                # 502/503 from the host while the instance boots; 0 = no answer yet.
                # A proxy or DNS failure is not a sleeping server: it fails at once.
                if exc.status not in (NET_TRANSIENT, 502, 503, 504) or clock() > deadline:
                    raise
            if first:
                on_status("waking")
                first = False
            sleep(5)

    def missions(self) -> list[dict]:
        return self._call("GET", "/api/missions")

    def submit(self, req: dict) -> str:
        return self._call("POST", "/api/predict", req)["job_id"]

    def job(self, job_id: str) -> dict:
        return self._call("GET", f"/api/jobs/{job_id}")

    def download(self, job_id: str, fmt: str) -> bytes:
        """A finished job as a file (ical, kml, csv, geojson); 404 once the job expired."""
        return self._raw("GET", f"/api/jobs/{job_id}/export/{fmt}", timeout_s=120.0)

    def run(self, req: dict, on_status: Callable[[str], None],
            is_canceled: Callable[[], bool],
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> tuple[str, dict]:
        """Wake the server, submit, poll until done. Returns (job_id, result)."""
        on_status("connecting")
        self.wake(on_status, is_canceled, sleep, clock)
        on_status("submitting")
        job_id = self.submit(req)
        on_status("computing")
        deadline = clock() + JOB_TIMEOUT_S
        for delay in poll_delays():
            if is_canceled():
                raise Canceled()
            job = self.job(job_id)
            if job["status"] == "done":
                return job_id, job.get("result") or {}
            if job["status"] == "error":
                raise ApiError(job.get("error") or "prediction failed")
            if clock() > deadline:
                raise ApiError("the server did not finish in time; try a smaller request")
            sleep(delay)
        raise AssertionError("unreachable")


# --- results -----------------------------------------------------------------

def evidence(row: dict) -> str:
    """Evidence code of a record; an unknown source stays 'unspecified', never 'planned'.

    Newer servers send the code as ``evidence``; the local mapping covers older ones.
    """
    code = row.get("evidence")
    if code in EVIDENCE_ORDER:
        return code
    return EVIDENCE_BY_SOURCE.get(row.get("source") or "", "unspecified")


def tasking_required(row: dict) -> bool:
    return bool(row.get("opportunity_only"))


def mission_group(key: str) -> str:
    if key.startswith("sentinel"):
        return "Sentinel"
    if key.startswith("landsat"):
        return "Landsat"
    if key in ("terra", "aqua"):
        return "MODIS"
    if key == "suomi-npp" or key.startswith("noaa-"):
        return "VIIRS"
    if key == "pace":
        return "PACE"
    if key in ("enmap", "prisma"):
        return "Hyperspectral"
    if key.startswith("cbers") or key.startswith("amazonia"):
        return "INPE"
    return "Other"


def join_rows(result: dict) -> list[tuple[dict, Optional[dict]]]:
    """Pair each overpass row with its footprint geometry (joined on ``id``)."""
    geoms = {f.get("id"): f.get("geometry")
             for f in (result.get("footprints") or {}).get("features", [])}
    return [(row, geoms.get(row.get("id"))) for row in result.get("overpasses", [])]


def _ring_wkt(ring: list) -> str:
    return "(" + ", ".join(f"{x:.7g} {y:.7g}" for x, y, *_ in ring) + ")"


def _polygon_wkt(rings: list) -> str:
    return "(" + ", ".join(_ring_wkt(r) for r in rings) + ")"


def geometry_to_multipolygon_wkt(geom: Optional[dict]) -> Optional[str]:
    """GeoJSON (Multi)Polygon → MultiPolygon WKT; None for anything else."""
    if not geom:
        return None
    kind = geom.get("type")
    if kind == "Polygon":
        polys = [geom["coordinates"]]
    elif kind == "MultiPolygon":
        polys = geom["coordinates"]
    elif kind == "GeometryCollection":
        polys = []
        for g in geom.get("geometries", []):
            if g.get("type") == "Polygon":
                polys.append(g["coordinates"])
            elif g.get("type") == "MultiPolygon":
                polys.extend(g["coordinates"])
    else:
        return None
    polys = [p for p in polys if p and p[0]]
    if not polys:
        return None
    return "MULTIPOLYGON (" + ", ".join(_polygon_wkt(p) for p in polys) + ")"


# --- passes ------------------------------------------------------------------

def _ts(row: dict) -> Optional[datetime]:
    iso = row.get("timestamp_utc")
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _identity(row: dict) -> tuple:
    return (row.get("mission"), row.get("satellite"), row.get("sensor"), row.get("source"))


def group_passes(rows: list[dict]) -> list[dict]:
    """Group records (tiles, scenes, path/rows, swath segments) into passes.

    A pass is one satellite imaging the area once. The rule mirrors the server's own
    pass key: satellite + source + date + datatake id, else + relative orbit (path).
    Records with neither (orbit-propagated missions) are joined by time continuity:
    the same satellite within half an orbit. Source is part of the identity, so a planned pass
    and its catalogue product, or a projection and its reference pass, stay apart.
    Every record id is kept, so a pass can always be expanded into its records.
    """
    keyed: dict[tuple, list[dict]] = {}
    loose: list[dict] = []
    for r in rows:
        # Landsat and INPE rows carry the scene's path/row in datatake_id (== grid_cell);
        # that names a scene, not a pass, so those fall through to the relative orbit.
        datatake = r.get("datatake_id")
        if datatake and datatake != r.get("grid_cell"):
            keyed.setdefault(_identity(r) + (r.get("date"), "datatake", datatake),
                             []).append(r)
        elif r.get("orbit_relative") is not None:
            keyed.setdefault(_identity(r) + (r.get("date"), "orbit", r["orbit_relative"]),
                             []).append(r)
        else:
            loose.append(r)
    members = [(v, False) for v in keyed.values()]
    loose.sort(key=lambda r: (tuple(str(x) for x in _identity(r)), r.get("timestamp_utc") or ""))
    current: list[dict] = []
    for r in loose:
        if current:
            prev, t, t0 = current[-1], _ts(r), _ts(current[-1])
            same = (_identity(prev) == _identity(r) and t is not None and t0 is not None
                    and (t - t0).total_seconds() <= HALF_ORBIT_S)
            if not same:
                members.append((current, True))
                current = []
        current.append(r)
    if current:
        members.append((current, True))

    passes = []
    for recs, by_time in members:
        recs = sorted(recs, key=lambda r: r.get("timestamp_utc") or "")
        first = recs[0]
        clouds = [r["cloud_cover"] for r in recs if r.get("cloud_cover") is not None]
        covs = [r["coverage_fraction"] for r in recs if r.get("coverage_fraction") is not None]
        passes.append({
            "mission": first.get("mission"),
            "satellite": first.get("satellite"),
            "sensor": first.get("sensor"),
            "source": first.get("source"),
            "evidence": evidence(first),
            "tasking_required": any(tasking_required(r) for r in recs),
            "start": first.get("timestamp_utc"),
            "end": recs[-1].get("timestamp_utc"),
            "row_ids": [r.get("id") for r in recs],
            "grid_cells": sorted({str(r["grid_cell"]) for r in recs if r.get("grid_cell")}),
            "cloud_range": (min(clouds), max(clouds)) if clouds else None,
            "max_record_coverage": max(covs) if covs else None,
            "projected_from": first.get("projected_from"),
            "grouped_by_time": by_time and len(recs) > 1,
        })
    passes.sort(key=lambda p: (p["start"] or "", str(p["satellite"])))
    for i, p in enumerate(passes):
        p["id"] = i
    return passes
