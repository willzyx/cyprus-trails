#!/usr/bin/env python3
"""Unit tests for tools/trails.py. Run: python3 tools/test_trails.py"""
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trails  # noqa: E402


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _svarint(value):
    return _varint((value << 1) ^ (value >> 63))


def encode_twkb(parts, precision=6, z_precision=1, with_z=True, with_m=True,
                bbox=False, size=False, ids=False):
    """Minimal TWKB encoder for LineString (one part) / MultiLineString (several)."""
    geom_type = 2 if len(parts) == 1 else 5
    ndims = 2 + with_z + with_m
    scales = [10 ** precision, 10 ** precision, 10 ** z_precision, 1][:2 + with_z] + ([1] if with_m else [])
    ints = [[[round(c * s) for c, s in zip(pt, scales)] for pt in part] for part in parts]

    body = bytearray()
    if bbox:
        for d in range(ndims):
            values = [pt[d] for part in ints for pt in part]
            body += _svarint(min(values)) + _svarint(max(values) - min(values))
    if geom_type == 5:
        body += _varint(len(parts))
        if ids:
            for i in range(len(parts)):
                body += _svarint(i + 100)
    prev = [0] * ndims
    for part in ints:
        body += _varint(len(part))
        for pt in part:
            for d in range(ndims):
                body += _svarint(pt[d] - prev[d])
            prev = pt

    head = bytes([geom_type | (((precision << 1) ^ (precision >> 31)) & 0x0F) << 4])
    meta = bbox | size << 1 | (ids and geom_type == 5) << 2 | (with_z or with_m) << 3
    out = bytearray(head) + bytes([meta])
    if with_z or with_m:
        out.append(with_z | with_m << 1 | z_precision << 2)
    if size:
        out += _varint(len(body))
    return bytes(out + body)


LINE = [(33.213804, 34.959362, 698.6, 1607502554000),
        (33.213739, 34.959357, 698.5, 1607502558000),
        (33.24947, 34.97395, 605.2, 1607510700000)]


class DecodeTwkbTest(unittest.TestCase):
    def test_linestring_with_z_and_m(self):
        parts = trails.decode_twkb(encode_twkb([LINE]))
        self.assertEqual(len(parts), 1)
        for got, want in zip(parts[0], LINE):
            self.assertEqual(len(got), 3)  # measure dropped
            for g, w in zip(got, want[:3]):
                self.assertAlmostEqual(g, w, places=6)

    def test_bbox_and_size_headers_are_skipped(self):
        parts = trails.decode_twkb(encode_twkb([LINE], bbox=True, size=True))
        self.assertAlmostEqual(parts[0][-1][0], 33.24947, places=6)
        self.assertAlmostEqual(parts[0][-1][2], 605.2, places=1)

    def test_multilinestring_deltas_continue_across_parts(self):
        first = [(33.0, 35.0, 100.0, 0), (33.001, 35.001, 105.0, 1)]
        second = [(33.5, 35.5, 200.0, 2), (33.501, 35.502, 190.0, 3)]
        parts = trails.decode_twkb(encode_twkb([first, second], ids=True))
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[1][0][0], 33.5, places=6)
        self.assertAlmostEqual(parts[1][1][1], 35.502, places=6)
        self.assertAlmostEqual(parts[1][1][2], 190.0, places=1)

    def test_2d_line(self):
        parts = trails.decode_twkb(encode_twkb([[(33.1, 35.1), (33.2, 35.2)]], with_z=False, with_m=False))
        self.assertEqual(parts, [[(33.1, 35.1), (33.2, 35.2)]])

    def test_truncated_data_raises(self):
        with self.assertRaises(trails.TrailError):
            trails.decode_twkb(encode_twkb([LINE])[:-3])

    def test_unsupported_type_raises(self):
        with self.assertRaises(trails.TrailError):
            trails.decode_twkb(bytes([0xC1, 0x00, 0x00, 0x00]))  # Point


class StatsTest(unittest.TestCase):
    def test_distance_of_one_degree_of_latitude(self):
        stats = trails.track_stats([[(33.0, 34.0), (33.0, 35.0)]])
        self.assertAlmostEqual(stats["distance_km"], 111.19, places=1)
        self.assertNotIn("ascent_m", stats)

    def test_ascent_hysteresis_ignores_small_wiggles(self):
        # points ~111 m apart, so the 50 m smoothing window holds a single point
        eles = [0, 2, 0, 2, 0, 10, 4, 20]
        part = [(33.0, 35.0 + i * 1e-3, e) for i, e in enumerate(eles)]
        stats = trails.track_stats([part])
        self.assertEqual(stats["ascent_m"], 26)
        self.assertEqual(stats["descent_m"], 6)
        self.assertEqual((stats["ele_min_m"], stats["ele_max_m"]), (0, 20))

    def test_dense_elevation_noise_is_smoothed_out(self):
        # flat ground, a point every ~5.5 m, elevation jumping ±10 m between points
        part = [(33.0, 35.0 + i * 5e-5, 100.0 + 10 * (i % 2)) for i in range(200)]
        stats = trails.track_stats([part])
        self.assertLessEqual(stats["ascent_m"], 3)
        self.assertLessEqual(stats["descent_m"], 3)

    def test_smoothed_elevations_keep_a_steady_climb(self):
        part = [(33.0, 35.0 + i * 5e-5, float(i)) for i in range(100)]  # +1 m every ~5.5 m
        smoothed = trails.smoothed_elevations(part)
        self.assertAlmostEqual(smoothed[50], 50.0, places=6)
        stats = trails.track_stats([part])
        self.assertTrue(90 <= stats["ascent_m"] <= 99, stats)  # the window trims a little at both ends
        self.assertEqual(stats["descent_m"], 0)


class UrlTest(unittest.TestCase):
    def test_canonical_url(self):
        info = trails.parse_wikiloc_url(
            "https://www.wikiloc.com/hiking-trails/agios-onoufrios-kapedes-61946569/?utm=x#map")
        self.assertEqual(info, {
            "url": "https://www.wikiloc.com/hiking-trails/agios-onoufrios-kapedes-61946569",
            "slug": "agios-onoufrios-kapedes", "num": "61946569", "activity": "hiking"})

    def test_rejects_other_sites(self):
        with self.assertRaises(trails.TrailError):
            trails.parse_wikiloc_url("https://example.com/hiking-trails/foo-123")


class PageTest(unittest.TestCase):
    SOURCE = trails.parse_wikiloc_url("https://www.wikiloc.com/hiking-trails/test-trail-1")

    def make_page(self, measures=True):
        data = {
            "mapData": [{"nom": "Test &amp; Trail", "loop": False,
                         "geom": base64.b64encode(encode_twkb([LINE])).decode()}],
            "waypoints": [{"name": "Spring", "lat": 34.96, "lon": 33.22, "elevation": 700,
                           "pictogramName": "Fountain", "photos": [{"url": "https://img"}]}],
        }
        page = ('<html><script>var mapData =' + json.dumps(data) + ';\nvar x = 1;</script>'
                '<span class="difficulty-label difficulty-label--2">Moderate</span>')
        if measures:
            stats = {"unitsAbrDistance": "mi", "unitsAbrHeight": "ft", "distance": 2.3,
                     "uphill": 1000.0, "downhill": 500.0, "loop": "Out and back"}
            page += '<script>var cfg = {\n  spaMeasures: ' + json.dumps(stats) + ',\n  other: 1 };</script>'
        return page + '</html>'

    def test_parse_page(self):
        page = trails.parse_page(self.make_page())
        self.assertEqual(page["name"], "Test & Trail")
        self.assertEqual(page["difficulty"], "Moderate")
        self.assertEqual(len(page["parts"][0]), 3)
        self.assertEqual(page["waypoints"], [
            {"name": "Spring", "type": "Fountain", "lon": 33.22, "lat": 34.96, "ele": 700.0}])
        self.assertEqual(page["climb"], {"ascent_m": 305, "descent_m": 152})  # feet -> metres
        self.assertEqual(page["trail_type"], "Out and back")

    def test_feature_collection_drops_timestamps_and_photos(self):
        page = trails.parse_page(self.make_page())
        fc = trails.build_feature_collection(page, self.SOURCE, page["name"], "2026-11-04")
        track = fc["features"][0]
        self.assertEqual(track["geometry"]["coordinates"][0], [33.213804, 34.959362, 698.6])
        self.assertEqual(track["properties"]["source_url"], self.SOURCE["url"])
        self.assertEqual((track["properties"]["ascent_m"], track["properties"]["descent_m"]), (305, 152))
        self.assertEqual(track["properties"]["trail_type"], "Out and back")
        self.assertFalse(track["properties"]["loop"])
        self.assertNotIn("photos", json.dumps(fc))
        self.assertNotIn("1607502554", json.dumps(fc))

    def test_climb_is_estimated_when_the_page_has_no_measures(self):
        page = trails.parse_page(self.make_page(measures=False))
        self.assertNotIn("climb", page)
        props = trails.build_feature_collection(page, self.SOURCE, page["name"], "2026-11-04")["features"][0]["properties"]
        self.assertEqual(props["ascent_m"], 0)
        self.assertGreater(props["descent_m"], 80)  # LINE drops from ~698 m to 605 m
        self.assertNotIn("trail_type", props)

    def test_page_without_track_data(self):
        with self.assertRaises(trails.TrailError):
            trails.parse_page("<html>nothing here</html>")


class JsonFormatTest(unittest.TestCase):
    def test_short_containers_inline_and_roundtrip(self):
        value = {"a": {"status": "todo"}, "coords": [[33.1, 35.1, 1.0]] * 50}
        text = trails.to_json(value)
        self.assertIn('"a": {"status": "todo"}', text)
        self.assertIn("    [33.1, 35.1, 1.0],\n", text)
        self.assertEqual(json.loads(text), value)

    def test_top_level_is_always_multiline(self):
        self.assertEqual(trails.to_json({"x": {"status": "todo"}}), '{\n  "x": {"status": "todo"}\n}\n')


if __name__ == "__main__":
    unittest.main()
