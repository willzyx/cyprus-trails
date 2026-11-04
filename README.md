# Cyprus Trails

A personal map of hiking trails in Cyprus showing which ones I have already
walked and which are still ahead. Every trail is drawn as a band 50 m either
side of the track (100 m wide), so together with live GPS the map shows at a
glance whether I am on a known trail and which areas I have already covered.

**Live: <https://willzyx.github.io/cyprus-trails/>**

## Features

- Trails drawn as ±50 m bands whose width is true to scale at every zoom level,
  purple for done and orange for not done yet. Done bands sit on top, so where a
  planned trail overlaps one already walked, the shared part shows as covered.
- A thin centerline on every band for following the exact track.
- Popup per trail with distance, ascent/descent, elevation range, difficulty,
  trail type (loop, one way, out and back) and a link to the original on Wikiloc.
- Waypoints from the original trail, shown from zoom 14.
- Live geolocation via `watchPosition`, with an accuracy circle. The bottom panel
  shows the totals (trails and km done) and the nearest trail: "On trail" when
  you are inside its band, otherwise the distance to it. 📍 centers the map on
  you and follows you until you drag the map.
- OpenStreetMap, OpenTopoMap (contour lines) and Esri satellite base maps.

## Running locally

The app is plain static HTML with no build step, but it fetches the trail files,
so it has to be served over HTTP (geolocation also needs a secure context —
`localhost` counts):

```sh
python3 -m http.server 8000
# open http://localhost:8000/
```

## Managing trails

Everything is done with [`tools/trails.py`](tools/trails.py), which needs only
Python 3.8+ and no packages. After any change, commit and push; GitHub Pages
publishes the new data within a minute or so — the page itself never needs to be
regenerated.

```sh
# Add trails (any number of URLs). Each one becomes trails/<id>.geojson with status "todo"
# (or pass --status done); the id is the URL slug, override it with --id, and the name with --name.
python3 tools/trails.py import https://www.wikiloc.com/hiking-trails/dipotamos-reservoir-163364159

# If Wikiloc refuses the download, save the page from a browser and import the file:
python3 tools/trails.py import URL --html saved-page.html

# Own routes: draw one on https://gpx.studio (it snaps to paths and adds elevation),
# export it as GPX and import the file. Recorded tracks (OsmAnd, Strava, …) work the same way.
# The id defaults to the file name; gpx.studio names files "New file 1", so pass --name.
python3 tools/trails.py import ~/Downloads/route_01.gpx --name "Akamas ridge"

# Mark progress
python3 tools/trails.py mark dipotamos-reservoir done
python3 tools/trails.py mark dipotamos-reservoir todo

python3 tools/trails.py list            # status, km and ascent of every trail
python3 tools/trails.py refresh --all   # re-import Wikiloc trails from the saved links, keeping names and status
python3 tools/trails.py remove ID       # delete a trail
python3 tools/trails.py check           # validate the data before committing
```

Run the tests with `python3 tools/test_trails.py`.

## How it works

### Data

- [`trails/index.json`](trails/index.json) lists every trail and its status —
  `{"status": "todo"}` or `{"status": "done"}` — keyed by trail id. It is the
  only file that changes when marking progress, and it can also be edited by
  hand (then run `trails.py check`).
- `trails/<id>.geojson` holds one trail as a GeoJSON `FeatureCollection`: one
  `track` feature (a `LineString` of `[lon, lat, elevation]` points with name,
  distance, ascent/descent, elevation range and loop flag; Wikiloc trails also
  have `source_url`, difficulty and trail type) plus one `waypoint` feature per
  waypoint. GitHub previews these files on a map.

### Import

A Wikiloc trail page embeds its track as `mapData` JSON in the HTML. The track
geometry is base64-encoded [TWKB](https://github.com/TWKB/Specification)
(Tiny Well-Known Binary): a LineString of zigzag-varint coordinate deltas with
elevation in Z (0.1 m precision) and timestamps in M. `trails.py` decodes it,
drops the timestamps and computes the distance as a sum of haversine distances.

Ascent/descent and the trail type are taken from the page's `spaMeasures` (the
figures Wikiloc shows, converted from feet if needed). Many tracks carry raw GPS
elevation that jumps by 10–30 m between neighbouring points, and no fixed
smoothing recovers a sensible total from it for every track. Only when a page
has no such figures does `trails.py` estimate them itself, from the elevation
averaged over 50 m of track and counted with a 3 m hysteresis.

Photos, descriptions and author details are not copied.

A GPX file is read with the standard-library XML parser (files with a DOCTYPE are
refused): every track segment and route becomes a line, `<wpt>` elements become
waypoints, and timestamps are dropped. Its ascent/descent is always estimated as
above; routes from gpx.studio carry elevation from a terrain model, which is
smooth, so the estimate is accurate.

### Map

Everything lives in [`index.html`](index.html): markup, styles and a single
vanilla-JS IIFE, with [Leaflet 1.9.4](https://leafletjs.com/) loaded from a CDN.
On load it fetches `trails/index.json` and then every trail file.

A band is a polyline with round caps and joins whose stroke width is set to
100 m in screen pixels (`2 × 50 m / metres-per-pixel` at the current zoom and
latitude, recomputed after every zoom). Such a stroke has exactly the shape of a
50 m buffer around the line, and Web Mercator is conformal, so the band is true
to scale without computing any polygons. Bands are drawn opaque inside a
semi-transparent map pane, so overlapping bands do not darken each other.

The distance to the nearest trail is the minimum point-to-segment distance in a
local equirectangular projection around the current position.

## Data

Most trail tracks, names and waypoints are imported from public trails on
[Wikiloc](https://www.wikiloc.com/); each such trail file and popup links to its
original. That track data is **not** covered by this repository's license; its
copyright remains with the respective authors. Trails without a Wikiloc link
were drawn by me.

This is a personal tool with no warranty. Trails change, GPS tracks can be
inaccurate — check conditions on the ground.

## License

The code in this repository is released under the [MIT License](LICENSE).
