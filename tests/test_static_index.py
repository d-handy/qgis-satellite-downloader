"""Offline checks of the static catalogue index and its use in stac.search."""

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

import qgis_app

qgis_app.start()

from satellite_downloader.core import catalog, stac, static_index
from satellite_downloader.core.catalog import COLLECTIONS, PROVIDERS, Collection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_static_index

ROWS = {
    "dem": [
        ["cologne", [6.5, 50.5, 7.5, 51.5], None, None, {"dem": "https://x/c.tif"}],
        ["fiji", [179.5, -17.0, -179.5, -16.0], None, None, {"dem": "https://x/f.tif"}],
    ],
    "monthly": [
        [
            "2020-01",
            [-180, -60, 180, 85],
            "2020-01-01T00:00:00Z",
            "2020-01-31T00:00:00Z",
            {"data": "https://x/1.tif"},
        ],
        [
            "2020-02",
            [-180, -60, 180, 85],
            "2020-02-01T00:00:00Z",
            "2020-02-29T00:00:00Z",
            {"data": "https://x/2.tif"},
        ],
    ],
    # One acquisition day, no end date
    "single": [
        [
            "2020-03-15",
            [-180, -60, 180, 85],
            "2020-03-15T00:00:00Z",
            None,
            {"data": "https://x/s.tif"},
        ],
    ],
}


def collection(cid, time_filter):
    return Collection(
        key=f"test:{cid}",
        label=cid,
        provider="test",
        collection_id=cid,
        assets=("dem",) if cid == "dem" else ("data",),
        default_assets=("dem",) if cid == "dem" else ("data",),
        native_res=30.0,
        time_filter=time_filter,
        has_cloud_cover=False,
    )


def square(w, s, e, n):
    ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    return {"type": "Polygon", "coordinates": [ring]}


class StaticIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        with gzip.open(static_index.items_file("test", cls.dir), "wt") as f:
            json.dump(ROWS, f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def find(self, cid, bbox, start="2000-01-01", end="2030-01-01", time_filter=False):
        env = [(bbox[0], bbox[2], bbox[1], bbox[3])]
        return static_index.search(
            collection(cid, time_filter), env, start, end, self.dir
        )

    def test_bbox_filter(self):
        self.assertEqual(
            [i["id"] for i in self.find("dem", (6.9, 50.9, 7.0, 51.0))], ["cologne"]
        )
        self.assertEqual(self.find("dem", (20, 20, 21, 21)), [])

    def test_touching_bboxes_count_as_intersecting(self):
        # Cologne tile: 6.5–7.5 °E, 50.5–51.5 °N; each AOI touches one edge
        for aoi in (
            (6.0, 50.8, 6.5, 51.0),  # west
            (7.5, 50.8, 8.0, 51.0),  # east
            (6.8, 50.0, 7.0, 50.5),  # south
            (6.8, 51.5, 7.0, 52.0),  # north
        ):
            with self.subTest(aoi):
                self.assertEqual([i["id"] for i in self.find("dem", aoi)], ["cologne"])

    def test_antimeridian_footprint_ends_exactly_at_180(self):
        fiji = self.find("dem", (179.8, -16.6, 179.9, -16.5))[0]
        lons = [
            p[0]
            for polygon in fiji["geometry"]["coordinates"]
            for ring in polygon
            for p in ring
        ]
        self.assertEqual(max(lons), 180.0)
        self.assertEqual(min(lons), -180.0)
        self.assertEqual(sorted(set(lons)), [-180.0, -179.5, 179.5, 180.0])

    def test_period_boundaries(self):
        # The period includes its first and last day.
        jan_end = self.find("monthly", (0, 0, 1, 1), "2020-01-31", "2020-01-31", True)
        self.assertEqual([i["id"] for i in jan_end], ["2020-01"])
        feb_start = self.find("monthly", (0, 0, 1, 1), "2020-01-15", "2020-02-01", True)
        self.assertEqual([i["id"] for i in feb_start], ["2020-02", "2020-01"])

    def test_item_without_end_date_covers_its_start_day_only(self):
        def ids(start, end):
            return [
                i["id"] for i in self.find("single", (0, 0, 1, 1), start, end, True)
            ]

        self.assertEqual(ids("2020-03-15", "2020-03-15"), ["2020-03-15"])
        self.assertEqual(ids("2020-03-01", "2020-03-14"), [])
        self.assertEqual(ids("2020-03-16", "2020-03-31"), [])
        item = self.find("single", (0, 0, 1, 1), time_filter=True)[0]
        self.assertNotIn("start_datetime", item["properties"])

    def test_static_products_ignore_the_period(self):
        found = self.find("monthly", (0, 0, 1, 1), "2025-01-01", "2025-01-02", False)
        self.assertEqual(len(found), 2)

    def test_antimeridian_item(self):
        east = self.find("dem", (179.8, -16.6, 179.9, -16.5))
        west = self.find("dem", (-179.9, -16.6, -179.8, -16.5))
        self.assertEqual([i["id"] for i in east], ["fiji"])
        self.assertEqual([i["id"] for i in west], ["fiji"])
        self.assertEqual(east[0]["geometry"]["type"], "MultiPolygon")

    def test_period_and_order(self):
        both = self.find("monthly", (0, 0, 1, 1), time_filter=True)
        self.assertEqual([i["id"] for i in both], ["2020-02", "2020-01"])
        feb = self.find("monthly", (0, 0, 1, 1), "2020-02-10", "2020-02-12", True)
        self.assertEqual([i["id"] for i in feb], ["2020-02"])
        self.assertEqual(feb[0]["properties"]["start_datetime"], "2020-02-01T00:00:00Z")

    def test_static_tiles_share_one_date_and_keep_their_ring(self):
        c = Collection(
            key="test:dem",
            label="dem",
            provider="test",
            collection_id="dem",
            assets=("dem",),
            default_assets=("dem",),
            native_res=30.0,
            time_filter=False,
            interval=("2008-01-01T00:00:00Z", None),
        )
        ring = [[10, 78], [16, 77.5], [18, 78.5], [11, 79], [10, 78]]
        a = static_index.to_item(
            ["a", [10, 77.5, 18, 79], "2011-06-04T00:00:00Z", None, {}, ring], c
        )
        b = static_index.to_item(
            ["b", [10, 77.5, 18, 79], "2010-03-29T00:00:00Z", None, {}], c
        )
        self.assertEqual(stac.item_datetime(a), stac.item_datetime(b))
        self.assertEqual(stac.item_datetime(a), "2008-01-01T00:00:00Z")
        self.assertEqual(a["geometry"]["coordinates"], [ring])
        self.assertEqual(b["geometry"]["type"], "Polygon")

    def test_items_look_like_stac(self):
        item = self.find("dem", (6.9, 50.9, 7.0, 51.0))[0]
        self.assertEqual(item["assets"]["dem"]["href"], "https://x/c.tif")
        self.assertEqual(item["collection"], "dem")


class StacSearchStaticTest(unittest.TestCase):
    def test_search_dispatches_to_index(self):
        pgc = next((c for c in COLLECTIONS if c.provider == "pgc"), None)
        if pgc is None:
            self.skipTest("no PGC index built")
        items, _ = stac.search(
            PROVIDERS["pgc"],
            pgc,
            square(15.5, 78.1, 15.8, 78.3),  # Longyearbyen
            "2020-01-01",
            "2020-12-31",
            None,
            5,
        )
        self.assertTrue(items)
        self.assertGreater(items[0][stac.COVERAGE_KEY], 0)
        self.assertTrue(
            items[0]["assets"][pgc.default_assets[0]]["href"].startswith("https://")
        )


class BuilderHelpersTest(unittest.TestCase):
    def test_swapped_bbox(self):
        fixed = build_static_index.fix_bbox([-62.0, -180.0, 87.37, 179.98], None)
        self.assertEqual(fixed, [-180.0, -62.0, 179.98, 87.37])

    def test_bbox_from_geometry_and_3d(self):
        self.assertEqual(
            build_static_index.fix_bbox(None, square(1, 2, 3, 4)), [1.0, 2.0, 3.0, 4.0]
        )
        self.assertEqual(
            build_static_index.fix_bbox([1, 2, 0, 3, 4, 9], None), [1.0, 2.0, 3.0, 4.0]
        )

    def test_simple_ring_only_for_tilted_small_polygons(self):
        tilted = {
            "type": "Polygon",
            "coordinates": [[[10, 78], [16, 77.5], [18, 78.5], [10, 78]]],
        }
        self.assertIsNotNone(build_static_index.simple_ring(tilted))
        wrapped = {"type": "MultiPolygon", "coordinates": [tilted["coordinates"]]}
        self.assertEqual(
            build_static_index.simple_ring(wrapped),
            build_static_index.simple_ring(tilted),
        )
        self.assertIsNone(build_static_index.simple_ring(square(1, 2, 3, 4)))
        many = [[i / 10, (i % 2) / 10] for i in range(40)] + [[0, 0]]
        self.assertIsNone(
            build_static_index.simple_ring({"type": "Polygon", "coordinates": [many]})
        )

    def test_space_separated_datetimes(self):
        self.assertEqual(
            build_static_index.iso("2023-05-22 04:35:08Z"), "2023-05-22T04:35:08Z"
        )


class ProvidersTest(unittest.TestCase):
    def test_static_providers_have_sources(self):
        static = {
            k for k, p in PROVIDERS.items() if p.search_method == catalog.SEARCH_STATIC
        }
        self.assertEqual(static, set(build_static_index.SOURCES))

    def test_static_entries_are_loaded(self):
        for path in (catalog.GENERATED_FILE.parent / catalog.STATIC_DIR).glob("*.json"):
            provider = path.name.split(".")[0]
            with self.subTest(provider):
                self.assertTrue(static_index.items_file(provider).exists())
                ids = {c.collection_id for c in COLLECTIONS if c.provider == provider}
                self.assertEqual(ids, set(static_index.load(provider)))


if __name__ == "__main__":
    unittest.main()
