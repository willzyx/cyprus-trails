#!/usr/bin/env python3
"""Command-line tool for the Cyprus Trails map.

Imports public Wikiloc trails into one GeoJSON file per trail under
``trails/`` and maintains ``trails/index.json``, the list of trails together
with their done/todo status. Uses the Python standard library only.

    python3 tools/trails.py import URL [URL ...] [--id ID] [--name NAME] [--html FILE] [--force]
    python3 tools/trails.py refresh ID [ID ...] | --all
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
import urllib.error
import urllib.parse
import urllib.request
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
        "source_url": source["url"],
        "activity": source["activity"],
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
                 page_html: str | None = None, force: bool = False, keep_name: bool = False) -> str:
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
    collection = build_feature_collection(page, source, name or page["name"] or trail_id,
                                          dt.date.today().isoformat())
    write_json(path, collection)

    index = load_index()
    index.setdefault(trail_id, {"status": "todo"})
    save_index(index)

    props = collection["features"][0]["properties"]
    npoints = sum(len(p) for p in page["parts"])
    climb = f", ↑{props['ascent_m']} m ↓{props['descent_m']} m" if "ascent_m" in props else ""
    print(f"{trail_id}: \"{props['name']}\" — {props['distance_km']} km{climb}, "
          f"{npoints} points, {len(page['waypoints'])} waypoints [{index[trail_id]['status']}]")
    return trail_id


def cmd_import(args) -> int:
    if len(args.urls) > 1 and (args.id or args.name or args.html):
        raise TrailError("--id, --name and --html work with a single URL only")
    page_html = Path(args.html).read_text(encoding="utf-8", errors="replace") if args.html else None
    failed = 0
    for url in args.urls:
        try:
            import_trail(url, trail_id=args.id, name=args.name, page_html=page_html, force=args.force)
        except TrailError as exc:
            print(f"error: {url}: {exc}", file=sys.stderr)
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
            url = load_track_properties(trail_id).get("source_url")
            if not url:
                raise TrailError("no trail file with a source_url")
            import_trail(url, trail_id=trail_id, force=True, keep_name=True)
        except TrailError as exc:
            print(f"error: {trail_id}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


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
            try:
                parse_wikiloc_url(props.get("source_url") or "")
            except TrailError:
                errors.append(f"{where}: missing or invalid source_url")
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
        if file_errors:
            continue
        num = parse_wikiloc_url(load_track_properties(trail_id)["source_url"])["num"]
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

    p = sub.add_parser("import", help="import trails from Wikiloc URLs")
    p.add_argument("urls", nargs="+", metavar="URL")
    p.add_argument("--id", help="trail id / file name (default: the URL slug)")
    p.add_argument("--name", help="display name (default: the name on Wikiloc)")
    p.add_argument("--html", metavar="FILE", help="read a page saved from the browser instead of downloading it")
    p.add_argument("--force", action="store_true", help="overwrite an existing trail file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("refresh", help="re-import trails from their saved source_url, keeping name and status")
    p.add_argument("ids", nargs="*", metavar="ID")
    p.add_argument("--all", action="store_true", help="refresh every trail")
    p.set_defaults(func=cmd_refresh)

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
