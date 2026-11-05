#!/usr/bin/env python3
"""Command-line tool for the Cyprus Trails map.

Imports public Wikiloc trails and GPX files (e.g. routes drawn on gpx.studio)
into one GeoJSON file per trail under ``trails/`` and maintains
``trails/index.json``, the list of trails together with their done/todo status.
Uses the Python standard library only.

    python3 tools/trails.py import URL|FILE.gpx [...] [--id ID] [--name NAME] [--html FILE] [--force] [--status done|todo]
    python3 tools/trails.py refresh ID [ID ...] | --all
    python3 tools/trails.py patch ID FILE.gpx [--max-gap METRES]
    python3 tools/trails.py mark ID done|todo
    python3 tools/trails.py list
    python3 tools/trails.py remove ID
    python3 tools/trails.py check
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import math
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAILS_DIR = ROOT / "trails"
INDEX_PATH = TRAILS_DIR / "index.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
EARTH_RADIUS_M = 6371008.8
ELE_SMOOTHING_M = 50.0     # elevation is averaged over this stretch of track before ascent/descent...
ASCENT_THRESHOLD_M = 3.0   # ...and counted with this hysteresis, so GPS elevation noise does not add up
LOOP_MAX_GAP_M = 200.0     # start and end closer than this => loop
BAND_RADIUS_M = 50.0       # half-width of a trail's band on the map; also the default patch tolerance
JSON_WIDTH = 120           # containers shorter than this are written on one line
STATUSES = ("done", "todo")

URL_RE = re.compile(
    r"^https://(?:[a-z]{2,3}\.)?wikiloc\.com/"
    r"(?P<category>[a-z0-9-]+)/(?P<slug>[a-z0-9-]+)-(?P<num>\d+)$"
)
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAPDATA_RE = re.compile(r"\bmapData\s*=\s*\{")
MEASURES_RE = re.compile(r"\bspaMeasures\s*:\s*\{")
HEIGHT_TO_M = {"ft": 0.3048, "m": 1.0}
DIFFICULTY_RE = re.compile(r'difficulty-label--\d+">\s*([^<]+?)\s*<')


class TrailError(Exception):
    """A problem the user can fix; reported without a traceback."""


# --------------------------------------------------------------------------
# Wikiloc URL and page parsing
# --------------------------------------------------------------------------

def parse_wikiloc_url(url: str) -> dict:
    """Split a Wikiloc trail URL into its canonical form, slug, numeric id and activity."""
    parts = urllib.parse.urlsplit(url.strip())
    canonical = f"https://{parts.netloc.lower()}{parts.path.rstrip('/')}"
    match = URL_RE.match(canonical)
    if not match:
        raise TrailError(f"not a Wikiloc trail URL: {url}")
    category = match["category"]
    activity = category[: -len("-trails")] if category.endswith("-trails") else category
    return {"url": canonical, "slug": match["slug"], "num": match["num"], "activity": activity}


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise TrailError("track geometry is truncated")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def varint(self) -> int:
        result = shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if b < 0x80:
                return result
            shift += 7

    def svarint(self) -> int:
        return _unzigzag(self.varint())


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def decode_twkb(data: bytes) -> list[list[tuple]]:
    """Decode a TWKB (Tiny Well-Known Binary) LineString or MultiLineString.

    Returns one list of points per line part. Each point is ``(lon, lat)`` or
    ``(lon, lat, ele)``. Measures (Wikiloc stores timestamps there) are dropped.
    """
    r = _Reader(data)
    head = r.byte()
    geom_type, precision = head & 0x0F, _unzigzag(head >> 4)
    meta = r.byte()
    has_bbox, has_size, has_ids, has_ext, is_empty = (bool(meta >> bit & 1) for bit in range(5))
    has_z = has_m = False
    z_precision = 0
    if has_ext:
        ext = r.byte()
        has_z, has_m = bool(ext & 1), bool(ext & 2)
        z_precision = ext >> 2 & 7
    if has_size:
        r.varint()
    ndims = 2 + has_z + has_m
    if has_bbox:
        for _ in range(2 * ndims):
            r.svarint()
    if is_empty:
        return []

    if geom_type == 2:
        nparts = 1
    elif geom_type == 5:
        nparts = r.varint()
        if has_ids:
            for _ in range(nparts):
                r.svarint()
    else:
        raise TrailError(f"unsupported TWKB geometry type {geom_type} (expected a line)")

    xy_scale = 10.0 ** precision
    z_scale = 10.0 ** z_precision
    acc = [0] * ndims  # deltas continue across parts
    parts = []
    for _ in range(nparts):
        points = []
        for _ in range(r.varint()):
            for d in range(ndims):
                acc[d] += r.svarint()
            point = (acc[0] / xy_scale, acc[1] / xy_scale)
            if has_z:
                point += (acc[2] / z_scale,)
            points.append(point)
        parts.append(points)
    return parts


def parse_page(page_html: str) -> dict:
    """Extract name, track, waypoints and difficulty from a Wikiloc trail page."""
    match = MAPDATA_RE.search(page_html)
    if not match:
        raise TrailError("no track data on the page (is the trail public? did Wikiloc change its layout?)")
    try:
        data, _ = json.JSONDecoder().raw_decode(page_html, match.end() - 1)
    except json.JSONDecodeError as exc:
        raise TrailError(f"cannot parse the page's track data: {exc}") from None
    trails = data.get("mapData") or []
    if not trails or not trails[0].get("geom"):
        raise TrailError("the page has no track geometry")
    trail = trails[0]

    parts = [p for p in decode_twkb(base64.b64decode(trail["geom"])) if len(p) >= 2]
    if not parts:
        raise TrailError("the track geometry is empty")

    waypoints = []
    for wp in data.get("waypoints") or []:
        if wp.get("lat") is None or wp.get("lon") is None:
            continue
        waypoints.append({
            "name": html.unescape(str(wp.get("name") or "")).strip(),
            "type": wp.get("pictogramName") or None,
            "lon": float(wp["lon"]),
            "lat": float(wp["lat"]),
            "ele": float(wp["elevation"]) if wp.get("elevation") is not None else None,
        })

    difficulty = DIFFICULTY_RE.search(page_html)
    measures = parse_measures(page_html)
    return {
        "name": html.unescape(str(trail.get("nom") or "")).strip(),
        "loop": bool(trail.get("loop")),
        "difficulty": html.unescape(difficulty.group(1)).strip() if difficulty else None,
        "parts": parts,
        "waypoints": waypoints,
        **measures,
    }


def parse_measures(page_html: str) -> dict:
    """Wikiloc's own ascent/descent (converted to metres) and trail type, if the page has them.

    Wikiloc's figures are preferred over our estimate: many tracks carry raw GPS elevation
    whose noise no fixed smoothing handles well, and these are the numbers shown on the site.
    """
    match = MEASURES_RE.search(page_html)
    if not match:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(page_html, match.end() - 1)
    except json.JSONDecodeError:
        return {}
    result = {}
    trail_type = data.get("loop")
    if isinstance(trail_type, str) and trail_type.strip():
        result["trail_type"] = trail_type.strip()
    factor = HEIGHT_TO_M.get(data.get("unitsAbrHeight"))
    up, down = data.get("uphill"), data.get("downhill")
    if factor and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (up, down)):
        result["climb"] = {"ascent_m": round(up * factor), "descent_m": round(down * factor)}
    return result


def fetch_page(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise TrailError(
            f"HTTP {exc.code} while fetching {url}; save the page from a browser and pass it with --html"
        ) from None
    except urllib.error.URLError as exc:
        raise TrailError(f"cannot fetch {url}: {exc.reason}") from None


# --------------------------------------------------------------------------
# GPX files
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop the XML namespace


def _children(element, name: str) -> list:
    return [child for child in element if _local(child.tag) == name]


def _child_text(element, name: str) -> str:
    found = _children(element, name)
    return (found[0].text or "").strip() if found else ""


def _gpx_point(element) -> tuple:
    try:
        lat, lon = float(element.get("lat")), float(element.get("lon"))
        ele = _child_text(element, "ele")
        point = (lon, lat, float(ele)) if ele else (lon, lat)
    except (TypeError, ValueError):
        raise TrailError("the GPX file has a point with invalid coordinates") from None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise TrailError(f"the GPX file has a point out of range: lat={lat}, lon={lon}")
    return point


def parse_gpx(data: bytes) -> dict:
    """Extract tracks/routes (as line parts), waypoints and a name from a GPX file.

    Timestamps and any other per-point data besides elevation are dropped.
    """
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise TrailError("GPX files with a DOCTYPE are not supported")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise TrailError(f"not a valid GPX file: {exc}") from None
    if _local(root.tag) != "gpx":
        raise TrailError("not a GPX file (no <gpx> root element)")

    parts, names = [], []
    for trk in _children(root, "trk"):
        names.append(_child_text(trk, "name"))
        for segment in _children(trk, "trkseg"):
            parts.append([_gpx_point(p) for p in _children(segment, "trkpt")])
    for rte in _children(root, "rte"):
        names.append(_child_text(rte, "name"))
        parts.append([_gpx_point(p) for p in _children(rte, "rtept")])
    parts = [part for part in parts if len(part) >= 2]
    if not parts:
        raise TrailError("the GPX file has no track or route with at least two points")
    if not all(len(pt) == 3 for part in parts for pt in part):
        parts = [[pt[:2] for pt in part] for part in parts]  # elevation only if every point has it

    waypoints = []
    for wpt in _children(root, "wpt"):
        point = _gpx_point(wpt)
        waypoints.append({
            "name": _child_text(wpt, "name"),
            "type": _child_text(wpt, "type") or _child_text(wpt, "sym") or None,
            "lon": point[0],
            "lat": point[1],
            "ele": point[2] if len(point) > 2 else None,
        })

    metadata = _children(root, "metadata")
    name = next((n for n in names if n), "") or (_child_text(metadata[0], "name") if metadata else "")
    return {"name": name, "loop": False, "difficulty": None, "parts": parts, "waypoints": waypoints}


# --------------------------------------------------------------------------
# Statistics and GeoJSON
# --------------------------------------------------------------------------

def haversine_m(a: tuple, b: tuple) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def smoothed_elevations(part: list[tuple], window_m: float = ELE_SMOOTHING_M) -> list[float]:
    """Moving average of elevation over ``window_m`` metres of track centred on each point."""
    cum = [0.0]
    for i in range(1, len(part)):
        cum.append(cum[-1] + haversine_m(part[i - 1], part[i]))
    prefix = [0.0]
    for pt in part:
        prefix.append(prefix[-1] + pt[2])
    result = []
    lo = hi = 0
    for i in range(len(part)):
        while cum[i] - cum[lo] > window_m / 2:
            lo += 1
        while hi + 1 < len(part) and cum[hi + 1] - cum[i] <= window_m / 2:
            hi += 1
        result.append((prefix[hi + 1] - prefix[lo]) / (hi + 1 - lo))
    return result


def track_stats(parts: list[list[tuple]]) -> dict:
    distance = sum(haversine_m(p[i], p[i + 1]) for p in parts for i in range(len(p) - 1))
    stats = {"distance_km": round(distance / 1000, 2)}

    elevations = [pt[2] for part in parts for pt in part if len(pt) > 2]
    if elevations:
        ascent = descent = 0.0
        for part in parts:
            smoothed = smoothed_elevations(part)
            ref = smoothed[0]
            for ele in smoothed[1:]:
                dz = ele - ref
                if dz >= ASCENT_THRESHOLD_M:
                    ascent += dz
                    ref = ele
                elif dz <= -ASCENT_THRESHOLD_M:
                    descent -= dz
                    ref = ele
        stats.update({
            "ascent_m": round(ascent),
            "descent_m": round(descent),
            "ele_min_m": round(min(elevations), 1),
            "ele_max_m": round(max(elevations), 1),
        })
    return stats


def _coord(point: tuple) -> list:
    coord = [round(point[0], 6), round(point[1], 6)]
    if len(point) > 2 and point[2] is not None:
        coord.append(round(point[2], 1))
    return coord


def build_feature_collection(page: dict, source: dict, name: str, imported: str) -> dict:
    parts = page["parts"]
    if len(parts) == 1:
        geometry = {"type": "LineString", "coordinates": [_coord(p) for p in parts[0]]}
    else:
        geometry = {"type": "MultiLineString", "coordinates": [[_coord(p) for p in part] for part in parts]}

    stats = track_stats(parts)
    stats.update(page.get("climb") or {})
    properties = {
        "kind": "track",
        "name": name,
        "source_url": source.get("url"),
        "activity": source.get("activity"),
        "difficulty": page["difficulty"],
        **stats,
        "loop": page["loop"] or haversine_m(parts[0][0], parts[-1][-1]) < LOOP_MAX_GAP_M,
        "trail_type": page.get("trail_type"),
        "imported": imported,
    }
    features = [{
        "type": "Feature",
        "properties": {k: v for k, v in properties.items() if v is not None},
        "geometry": geometry,
    }]
    for wp in page["waypoints"]:
        props = {"kind": "waypoint", "name": wp["name"], "type": wp["type"]}
        features.append({
            "type": "Feature",
            "properties": {k: v for k, v in props.items() if v},
            "geometry": {"type": "Point", "coordinates": _coord((wp["lon"], wp["lat"], wp["ele"]))},
        })
    return {"type": "FeatureCollection", "features": features}


def to_json(value) -> str:
    """Pretty-print JSON, keeping short containers (e.g. each coordinate) on one line."""
    return _format(value, "", 0, top=True) + "\n"


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _format(value, indent: str, used: int, top: bool = False) -> str:
    if not isinstance(value, (dict, list)) or not value:
        return _dumps(value)
    inline = _dumps(value)
    if not top and used + len(inline) + 1 <= JSON_WIDTH:
        return inline
    child = indent + "  "
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            prefix = f"{child}{_dumps(key)}: "
            lines.append(prefix + _format(item, child, len(prefix)))
        return "{\n" + ",\n".join(lines) + "\n" + indent + "}"
    lines = [child + _format(item, child, len(child)) for item in value]
    return "[\n" + ",\n".join(lines) + "\n" + indent + "]"


# --------------------------------------------------------------------------
# Repository files
# --------------------------------------------------------------------------

def trail_path(trail_id: str) -> Path:
    return TRAILS_DIR / f"{trail_id}.geojson"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(to_json(value))


def read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    try:
        index = read_json(INDEX_PATH)
    except json.JSONDecodeError as exc:
        raise TrailError(f"trails/index.json is not valid JSON: {exc}") from None
    if not isinstance(index, dict):
        raise TrailError("trails/index.json must be a JSON object")
    return index


def save_index(index: dict) -> None:
    write_json(INDEX_PATH, dict(sorted(index.items())))


def track_feature(collection: dict) -> dict | None:
    for feature in collection.get("features") or []:
        if isinstance(feature, dict) and (feature.get("properties") or {}).get("kind") == "track":
            return feature
    return None


def load_track_properties(trail_id: str) -> dict:
    path = trail_path(trail_id)
    if not path.exists():
        return {}
    feature = track_feature(read_json(path))
    return (feature or {}).get("properties") or {}


def ids_by_source() -> dict:
    """Map the numeric Wikiloc id of every imported trail to our trail id."""
    result = {}
    for path in sorted(TRAILS_DIR.glob("*.geojson")):
        url = load_track_properties(path.stem).get("source_url")
        if url:
            try:
                result[parse_wikiloc_url(url)["num"]] = path.stem
            except TrailError:
                pass
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def import_trail(url: str, *, trail_id: str | None = None, name: str | None = None,
                 page_html: str | None = None, force: bool = False, keep_name: bool = False,
                 status: str | None = None) -> str:
    """Import one trail. ``status`` sets its status; by default a new trail is "todo" and an
    existing one keeps its status."""
    source = parse_wikiloc_url(url)
    existing = ids_by_source().get(source["num"])
    trail_id = trail_id or existing or source["slug"]
    if not ID_RE.match(trail_id):
        raise TrailError(f"invalid trail id '{trail_id}' (use lowercase letters, digits and dashes)")
    if existing and existing != trail_id:
        raise TrailError(f"this trail is already imported as '{existing}'")
    path = trail_path(trail_id)
    if path.exists() and not force:
        if existing:
            raise TrailError(f"'{trail_id}' is already imported; use `refresh {trail_id}` or --force to re-import")
        raise TrailError(f"trails/{trail_id}.geojson already exists for another trail; choose an id with --id")

    if keep_name and name is None:
        name = load_track_properties(trail_id).get("name")
    page = parse_page(page_html if page_html is not None else fetch_page(source["url"]))
    return save_trail(trail_id, page, source, name or page["name"] or trail_id, status)


def import_file(file: str, *, trail_id: str | None = None, name: str | None = None,
                force: bool = False, status: str | None = None) -> str:
    """Import a GPX file, e.g. a route drawn on gpx.studio or a track recorded on the phone."""
    path = Path(file).expanduser()
    if path.suffix.lower() != ".gpx":
        raise TrailError(f"{file}: expected a Wikiloc URL or a .gpx file")
    if not path.is_file():
        raise TrailError(f"no such file: {file}")
    trail_id = trail_id or slugify(path.stem)
    if not ID_RE.match(trail_id):
        raise TrailError(f"invalid trail id '{trail_id}'; choose one with --id")
    if trail_path(trail_id).exists() and not force:
        raise TrailError(f"trails/{trail_id}.geojson already exists; choose another id with --id, "
                         "or pass --force to replace it")
    page = parse_gpx(path.read_bytes())
    return save_trail(trail_id, page, {}, name or page["name"] or path.stem, status)


def save_trail(trail_id: str, page: dict, source: dict, name: str, status: str | None) -> str:
    """Write the trail file and its index entry; print a one-line summary."""
    collection = build_feature_collection(page, source, name, dt.date.today().isoformat())
    write_json(trail_path(trail_id), collection)

    index = load_index()
    if status:
        index[trail_id] = {"status": status}
    else:
        index.setdefault(trail_id, {"status": "todo"})
    save_index(index)

    props = collection["features"][0]["properties"]
    npoints = sum(len(p) for p in page["parts"])
    climb = f", ↑{props['ascent_m']} m ↓{props['descent_m']} m" if "ascent_m" in props else ""
    print(f"{trail_id}: \"{props['name']}\" — {props['distance_km']} km{climb}, "
          f"{npoints} points, {len(page['waypoints'])} waypoints [{index[trail_id]['status']}]")
    return trail_id


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def cmd_import(args) -> int:
    if len(args.sources) > 1 and (args.id or args.name or args.html):
        raise TrailError("--id, --name and --html work with a single URL or file only")
    if args.html and not _is_url(args.sources[0]):
        raise TrailError("--html goes with a Wikiloc URL, not a GPX file")
    page_html = Path(args.html).read_text(encoding="utf-8", errors="replace") if args.html else None
    failed = 0
    for source in args.sources:
        try:
            if _is_url(source):
                import_trail(source, trail_id=args.id, name=args.name, page_html=page_html,
                             force=args.force, status=args.status)
            else:
                import_file(source, trail_id=args.id, name=args.name, force=args.force, status=args.status)
        except TrailError as exc:
            print(f"error: {source}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def cmd_refresh(args) -> int:
    index = load_index()
    ids = sorted(index) if args.all else args.ids
    if not ids:
        raise TrailError("name the trails to refresh, or pass --all")
    failed = 0
    for trail_id in ids:
        try:
            props = load_track_properties(trail_id)
            if not props:
                raise TrailError("no such trail file")
            url = props.get("source_url")
            if not url:
                print(f"{trail_id}: skipped, imported from a file "
                      f"(re-import it with `import FILE --id {trail_id} --force`)")
                continue
            import_trail(url, trail_id=trail_id, force=True, keep_name=True)
        except TrailError as exc:
            print(f"error: {trail_id}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _line_length_m(points: list) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def splice(route: list, patch: list, max_gap_m: float = BAND_RADIUS_M) -> tuple[list, int, int]:
    """Replace the stretch of ``route`` between the patch's two ends with ``patch``.

    Each end of the patch joins the nearest route point within ``max_gap_m``. When the route
    passes that place more than once (an out-and-back trail), the passes whose stretch of
    route is closest in length to the patch win. The patch may be drawn in either direction.
    Returns the new route and the indices of the replaced stretch.
    """
    cum = [0.0]
    for i in range(1, len(route)):
        cum.append(cum[-1] + haversine_m(route[i - 1], route[i]))

    def candidates(point, label):
        """The nearest route point of every separate pass of the route near ``point``."""
        dist = [haversine_m(point, q) for q in route]
        near = [i for i, d in enumerate(dist) if d <= max_gap_m]
        if not near:
            raise TrailError(f"the patch {label} is {min(dist):.0f} m from the trail "
                             f"(at most {max_gap_m:.0f} m allowed)")
        passes = [[near[0]]]
        for i in near[1:]:
            if i == passes[-1][-1] + 1:
                passes[-1].append(i)
            else:
                passes.append([i])
        return [min(p, key=lambda i: dist[i]) for p in passes]

    patch_len = _line_length_m(patch)
    starts, ends = candidates(patch[0], "start"), candidates(patch[-1], "end")
    pairs = [(abs(abs(cum[j] - cum[i]) - patch_len), i, j) for i in starts for j in ends if i != j]
    if not pairs:
        raise TrailError("the patch starts and ends at the same point of the trail")
    _, i_start, i_end = min(pairs)
    if i_start > i_end:
        patch, i_start, i_end = patch[::-1], i_end, i_start
    return route[:i_start] + patch + route[i_end + 1:], i_start, i_end


def cmd_patch(args) -> int:
    path = trail_path(args.id)
    if not path.exists():
        raise TrailError(f"unknown trail '{args.id}' (see `trails.py list`)")
    collection = read_json(path)
    track = track_feature(collection)
    if not track or (track.get("geometry") or {}).get("type") != "LineString":
        raise TrailError("only a trail made of a single line can be patched")
    route = [tuple(c) for c in track["geometry"]["coordinates"]]

    file = Path(args.file).expanduser()
    if not file.is_file():
        raise TrailError(f"no such file: {args.file}")
    patch_parts = parse_gpx(file.read_bytes())["parts"]
    if len(patch_parts) != 1:
        raise TrailError("the patch file must contain exactly one track or route")
    patch = patch_parts[0]
    if len(patch[0]) != len(route[0]):
        raise TrailError("the patch and the trail must both have elevation, or both not")

    new_route, i_start, i_end = splice(route, patch, args.max_gap)
    old_km = _line_length_m(route[i_start:i_end + 1]) / 1000
    track["geometry"]["coordinates"] = [_coord(p) for p in new_route]

    props = track["properties"]
    stats = track_stats([new_route])
    if i_start == 0 or i_end == len(route) - 1:  # an end of the trail moved
        stats["loop"] = haversine_m(new_route[0], new_route[-1]) < LOOP_MAX_GAP_M
    for key in ("ascent_m", "descent_m", "ele_min_m", "ele_max_m"):
        if key not in stats:
            props.pop(key, None)
    updated = {key: stats.get(key, value) for key, value in props.items()}
    updated.update({key: value for key, value in stats.items() if key not in updated})
    track["properties"] = updated
    write_json(path, collection)

    climb = f", ↑{updated['ascent_m']} m ↓{updated['descent_m']} m" if "ascent_m" in updated else ""
    print(f"{args.id}: replaced {old_km:.2f} km ({i_end - i_start + 1} points) with "
          f"{_line_length_m(patch) / 1000:.2f} km ({len(patch)} points); "
          f"now {updated['distance_km']} km{climb}")
    if updated.get("source_url"):
        print("note: ascent/descent are now estimated; `refresh` would bring back the Wikiloc track")
    return 0


def cmd_mark(args) -> int:
    index = load_index()
    if args.id not in index:
        raise TrailError(f"unknown trail '{args.id}' (see `trails.py list`)")
    index[args.id] = {"status": args.status}
    save_index(index)
    print(f"{args.id}: {args.status}")
    return 0


def cmd_list(args) -> int:
    index = load_index()
    if not index:
        print("no trails yet — add one with `trails.py import URL`")
        return 0
    rows = []
    total = {"done": [0, 0.0], "todo": [0, 0.0]}
    for trail_id, entry in sorted(index.items()):
        props = load_track_properties(trail_id)
        status = entry.get("status", "?")
        km = props.get("distance_km") or 0.0
        if status in total:
            total[status][0] += 1
            total[status][1] += km
        rows.append((status, f"{km:.2f}", str(props.get("ascent_m", "")),
                     trail_id, props.get("name", "(missing file)")))
    header = ("STATUS", "KM", "UP M", "ID", "NAME")
    widths = [max(len(r[i]) for r in rows + [header]) for i in range(4)]
    for row in [header] + rows:
        print("  ".join(cell.rjust(w) if i in (1, 2) else cell.ljust(w)
                        for i, (cell, w) in enumerate(zip(row[:4], widths))) + "  " + row[4])
    n_all = total["done"][0] + total["todo"][0]
    km_all = total["done"][1] + total["todo"][1]
    print(f"\ndone {total['done'][0]}/{n_all} trails, {total['done'][1]:.1f}/{km_all:.1f} km")
    return 0


def cmd_remove(args) -> int:
    index = load_index()
    path = trail_path(args.id)
    if args.id not in index and not path.exists():
        raise TrailError(f"unknown trail '{args.id}'")
    index.pop(args.id, None)
    save_index(index)
    if path.exists():
        path.unlink()
    print(f"removed {args.id}")
    return 0


def _is_lonlat(coord) -> bool:
    return (isinstance(coord, list) and 2 <= len(coord) <= 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in coord)
            and -180 <= coord[0] <= 180 and -90 <= coord[1] <= 90)


def validate_trail_file(path: Path) -> list[str]:
    name = f"trails/{path.name}"
    try:
        collection = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{name}: cannot read: {exc}"]
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        return [f"{name}: not a GeoJSON FeatureCollection"]
    errors = []
    features = collection.get("features")
    if not isinstance(features, list):
        return [f"{name}: 'features' must be a list"]
    tracks = [f for f in features if isinstance(f, dict) and (f.get("properties") or {}).get("kind") == "track"]
    if len(tracks) != 1:
        errors.append(f"{name}: expected exactly one track feature, found {len(tracks)}")
    for i, feature in enumerate(features):
        where = f"{name}: feature {i}"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            errors.append(f"{where}: not a GeoJSON Feature")
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        gtype, coords = geometry.get("type"), geometry.get("coordinates")
        if props.get("kind") == "track":
            lines = [coords] if gtype == "LineString" else coords if gtype == "MultiLineString" else None
            if not isinstance(lines, list) or not all(
                    isinstance(line, list) and len(line) >= 2 and all(map(_is_lonlat, line)) for line in lines):
                errors.append(f"{where}: track needs a LineString/MultiLineString of [lon, lat, ele] points")
            if not isinstance(props.get("name"), str) or not props["name"].strip():
                errors.append(f"{where}: track has no name")
            if "source_url" in props:  # absent for trails imported from a GPX file
                try:
                    parse_wikiloc_url(str(props["source_url"]))
                except TrailError:
                    errors.append(f"{where}: invalid source_url")
        elif props.get("kind") == "waypoint":
            if gtype != "Point" or not _is_lonlat(coords):
                errors.append(f"{where}: waypoint needs a Point geometry")
        else:
            errors.append(f"{where}: unknown kind {props.get('kind')!r}")
    return errors


def cmd_check(args) -> int:
    errors = []
    index = load_index()
    files = {p.stem for p in TRAILS_DIR.glob("*.geojson")}
    for trail_id in sorted(set(index) - files):
        errors.append(f"index.json lists '{trail_id}' but trails/{trail_id}.geojson is missing")
    for trail_id in sorted(files - set(index)):
        errors.append(f"trails/{trail_id}.geojson is not listed in index.json")

    for trail_id, entry in sorted(index.items()):
        where = f"index.json: {trail_id}"
        if not ID_RE.match(trail_id):
            errors.append(f"{where}: invalid id (lowercase letters, digits and dashes)")
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry must be an object")
            continue
        if entry.get("status") not in STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(STATUSES)}")
        for key in sorted(set(entry) - {"status"}):
            errors.append(f"{where}: unexpected key {key!r}")

    seen = {}
    for trail_id in sorted(files):
        file_errors = validate_trail_file(trail_path(trail_id))
        errors.extend(file_errors)
        url = load_track_properties(trail_id).get("source_url")
        if file_errors or not url:
            continue
        num = parse_wikiloc_url(url)["num"]
        if num in seen:
            errors.append(f"'{trail_id}' and '{seen[num]}' are imported from the same Wikiloc trail")
        seen.setdefault(num, trail_id)

    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"{len(files)} trail files, {len(index)} index entries: "
          + (f"{len(errors)} problem(s)" if errors else "OK"))
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the trails shown on the Cyprus Trails map.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="import trails from Wikiloc URLs or GPX files")
    p.add_argument("sources", nargs="+", metavar="URL|FILE.gpx")
    p.add_argument("--id", help="trail id / file name (default: the URL slug or the GPX file name)")
    p.add_argument("--name", help="display name (default: the name on Wikiloc or in the GPX file)")
    p.add_argument("--html", metavar="FILE", help="read a page saved from the browser instead of downloading it")
    p.add_argument("--force", action="store_true", help="overwrite an existing trail file")
    p.add_argument("--status", choices=STATUSES, help="set the status (default: todo for new trails)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("refresh", help="re-import trails from their saved source_url, keeping name and status")
    p.add_argument("ids", nargs="*", metavar="ID")
    p.add_argument("--all", action="store_true", help="refresh every trail")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("patch", help="replace a stretch of a trail with a redrawn piece from a GPX file")
    p.add_argument("id")
    p.add_argument("file", metavar="FILE.gpx")
    p.add_argument("--max-gap", type=float, default=BAND_RADIUS_M, metavar="METRES",
                   help=f"how far each end of the patch may be from the trail (default: {BAND_RADIUS_M:.0f})")
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("mark", help="mark a trail as done or todo")
    p.add_argument("id")
    p.add_argument("status", choices=STATUSES)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("list", help="list trails with status and distance")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="delete a trail file and its index entry")
    p.add_argument("id")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("check", help="validate trails/index.json and the trail files")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TrailError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
