"""Offline checks of the generated catalogue and item normalisation."""

import dataclasses
import json
import re
import tempfile
import unittest
from pathlib import Path

import qgis_app

qgis_app.start()

from satellite_downloader.core import catalog, stac
from satellite_downloader.core.catalog import (
    COLLECTIONS,
    COLLECTIONS_BY_KEY,
    PROVIDERS,
    load_generated,
)
from satellite_downloader.gui.collection_picker import (
    covers,
    matches,
    period,
)


class LoadGeneratedTest(unittest.TestCase):
    """``load_generated`` on a small file instead of the shipped catalogue."""

    minimal = {
        "key": "earthsearch:test",
        "label": "Test",
        "provider": "earthsearch",
        "collection_id": "test",
        "assets": ["data", "QA"],
        "default_assets": ["data"],
        "native_res": 30,
        "mode": "cog",
    }

    def load(self, entries: list[dict]) -> list[catalog.Collection]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collections.json"
            path.write_text(json.dumps({"collections": entries}), encoding="utf-8")
            return load_generated(path)

    def test_missing_fields_get_defaults(self):
        (c,) = self.load([self.minimal])
        self.assertFalse(c.has_cloud_cover)
        self.assertTrue(c.time_filter)
        self.assertEqual(c.note, "")
        self.assertEqual(c.category, "Other")
        self.assertIsNone(c.mask)
        self.assertIsNone(c.bbox)
        self.assertEqual(c.interval, (None, None))
        self.assertEqual(c.categorical_assets, ())
        self.assertEqual(c.native_res, 30.0)

    def test_optional_fields_are_read(self):
        entry = {
            **self.minimal,
            "mask": {"asset": "QA", "kind": "bits", "invalid": [0], "cloudy": [3]},
            "categorical_assets": ["data"],
            "bbox": [5, 45, 10, 48],
            "interval": ["2020-01-01T00:00:00Z"],
        }
        (c,) = self.load([entry])
        self.assertEqual(c.mask, catalog.MaskSpec("QA", "bits", (0,), (3,)))
        self.assertEqual(c.categorical_assets, ("data",))
        self.assertTrue(c.is_categorical("data"))
        self.assertEqual(c.bbox, (5, 45, 10, 48))
        self.assertEqual(c.interval, ("2020-01-01T00:00:00Z", None))

    def test_unknown_providers_and_curated_duplicates_are_skipped(self):
        unknown = {**self.minimal, "key": "nowhere:x", "provider": "nowhere"}
        curated = {
            **self.minimal,
            "key": "pc:sentinel-2-l2a",
            "provider": "pc",
            "collection_id": "sentinel-2-l2a",
        }
        loaded = self.load([unknown, curated, self.minimal])
        self.assertEqual([c.key for c in loaded], ["earthsearch:test"])

    def test_missing_file_gives_no_collections(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_generated(Path(tmp) / "absent.json"), [])


class ProviderCapabilitiesTest(unittest.TestCase):
    def test_generic_apis_get_no_server_side_cloud_filter(self):
        # A "query" these APIs do not support would fail or be ignored silently.
        for key in ("dlr", "eodc", "kagis"):
            with self.subTest(key):
                self.assertFalse(PROVIDERS[key].supports_query)
                self.assertTrue(PROVIDERS[key].supports_sort)

    def test_static_catalogues_are_searched_locally(self):
        for key in ("pgc", "openlandmap"):
            with self.subTest(key):
                self.assertEqual(PROVIDERS[key].search_method, catalog.SEARCH_STATIC)
                self.assertFalse(PROVIDERS[key].supports_query)


class GeneratedCatalogTest(unittest.TestCase):
    def test_generated_file_is_loaded(self):
        generated = load_generated()
        self.assertGreater(len(generated), 150)
        self.assertEqual(len(COLLECTIONS), len(catalog.CURATED) + len(generated))

    def test_keys_unique_and_curated_not_duplicated(self):
        self.assertEqual(len(COLLECTIONS_BY_KEY), len(COLLECTIONS))
        pairs = [(c.provider, c.collection_id) for c in COLLECTIONS]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_every_entry_is_usable(self):
        for c in COLLECTIONS:
            with self.subTest(c.key):
                self.assertIn(c.provider, PROVIDERS)
                self.assertTrue(c.assets)
                self.assertTrue(c.default_assets)
                self.assertTrue(
                    set(c.default_assets) <= set(c.assets), c.default_assets
                )
                self.assertGreater(c.native_res, 0)
                self.assertIn(c.mode, ("cog", "raw"))
                self.assertTrue(c.category)
                if c.mask:
                    self.assertIn(c.mask.asset, c.assets)
                for asset in c.bands.values():
                    self.assertIn(asset, c.assets)
                for pattern in c.asset_patterns.values():
                    re.compile(pattern)

    def test_expected_sensors_present(self):
        ids = {c.collection_id for c in COLLECTIONS}
        for expected in (
            "hls2-s30",
            "hls2-l30",
            "landsat-c2-l1",
            "aster-l1t",
            "modis-13Q1-061",
            "alos-palsar-mosaic",
            "naip",
            "esa-worldcover",
            "CB4-WFI-L4-SR-1",
            "AMZ1-WFI-L4-SR-1",
            "RESOURCESAT1_LISS3_L2_DN-1",
            "sentinel-2-c1-l2a",
            "s2_l2a",
            "ls9_sr",
            "gm_s2_annual",
            "ch.swisstopo.swissalti3d",
        ):
            self.assertIn(expected, ids)

    def test_resolution_of_geographic_sources_is_not_latitude_scaled(self):
        worldcover = COLLECTIONS_BY_KEY["pc:esa-worldcover"]
        self.assertAlmostEqual(worldcover.native_res, 10, delta=1)

    def test_categories_use_whole_words(self):
        swisseo = next(
            c
            for c in COLLECTIONS
            if c.collection_id.startswith("ch.swisstopo.swisseo_s2")
        )
        self.assertEqual(swisseo.category, "Optical")


class NormalizeItemTest(unittest.TestCase):
    def test_deafrica_s3_hrefs_become_https(self):
        item = {"assets": {"B04": {"href": "s3://deafrica-sentinel-2/a/B04.tif"}}}
        c = next(c for c in COLLECTIONS if c.provider == "deafrica")
        stac.normalize_item(item, PROVIDERS["deafrica"], c)
        self.assertEqual(
            item["assets"]["B04"]["href"],
            "https://deafrica-sentinel-2.s3.af-south-1.amazonaws.com/a/B04.tif",
        )

    def test_swisstopo_virtual_keys(self):
        c = COLLECTIONS_BY_KEY["swisstopo:ch.swisstopo.swissalti3d"]
        item = {
            "assets": {
                "swissalti3d_2019_2683-1247_0.5_2056_5728.tif": {"href": "a"},
                "swissalti3d_2019_2683-1247_2_2056_5728.tif": {"href": "b"},
            }
        }
        stac.normalize_item(item, PROVIDERS["swisstopo"], c)
        self.assertEqual(item["assets"]["0.5 m"]["href"], "a")
        self.assertEqual(item["assets"]["2 m"]["href"], "b")


class BandInferenceTest(unittest.TestCase):
    def setUp(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import build_catalog

        self.infer = build_catalog.infer_bands

    def test_landsat_numbering_differs_between_oli_and_tm(self):
        keys = ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7", "QA_PIXEL"]
        oli = self.infer("ls9_sr", keys)
        tm = self.infer("ls5_sr", keys)
        self.assertEqual(
            (oli["red"], oli["green"], oli["blue"]), ("SR_B4", "SR_B3", "SR_B2")
        )
        self.assertEqual(
            (tm["red"], tm["green"], tm["blue"]), ("SR_B3", "SR_B2", "SR_B1")
        )
        self.assertEqual(tm["nir"], "SR_B4")

    def test_sentinel2_scheme_and_common_name_keys(self):
        s2 = self.infer("s2_l2a", ["B02", "B03", "B04", "B08", "SCL"])
        self.assertEqual((s2["red"], s2["nir"]), ("B04", "B08"))
        inpe = self.infer("landsat-2", ["red", "green", "blue", "nir08", "qa_pixel"])
        self.assertEqual(inpe["red"], "red")

    def test_generated_defaults_are_true_colour_where_possible(self):
        for key in (
            "deafrica:ls9_sr",
            "deafrica:ls5_sr",
            "deafrica:gm_s2_annual",
            "bdc:landsat-2",
        ):
            c = COLLECTIONS_BY_KEY[key]
            self.assertEqual(
                list(c.default_assets),
                [c.bands["red"], c.bands["green"], c.bands["blue"]],
                key,
            )
        self.assertEqual(COLLECTIONS_BY_KEY["deafrica:s2_l2a"].native_res, 10)


class GeneratorRulesTest(unittest.TestCase):
    def setUp(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import build_catalog

        self.bc = build_catalog

    def test_categories_of_new_sources(self):
        cases = {
            ("dop20", "LGLN – DOP RGBI 20 Niedersachsen"): "Aerial imagery",
            (
                "MNE",
                "WorldPop population – Montenegro (MNE)",
            ): "Population and settlement",
            (
                "s1_rtc_gamma0",
                "SENTINEL1 Radiometric Terrain Corrected Gamma Nought",
            ): "Radar",
            ("gome2-h2o", "MetOp-A/B/C GOME-2 L3 P1D Water Vapor (H2O)"): "Atmosphere",
            ("ctm", "Crop Type Maps (latest)"): "Land cover and land use",
            (
                "hrdem",
                "Mosaic of High Resolution Digital Elevation Model (HRDEM) at 1m",
            ): ("Elevation and terrain"),
            ("vmi", "Volume, spruce saw timber (Paituli)"): "Vegetation and biomass",
            ("predicted-damage", "Predicted Building Damage: Earthquake"): "Events",
        }
        for (cid, title), expected in cases.items():
            with self.subTest(title):
                self.assertEqual(self.bc.category_for(cid, title), expected)

    def test_numbered_asset_keys_become_stable(self):
        assets = {"5016-56_COG": {"href": "a"}, "5016-56": {"href": "b"}}
        others = [{"assets": {"4121-19_COG": {}, "4121-19": {}}}]
        renamed, patterns = self.bc.numbered_keys(assets, others)
        self.assertEqual(set(renamed), {"COG", "file"})
        self.assertRegex("4121-19_COG", patterns["COG"])
        self.assertNotRegex("4121-19_COG", patterns["file"])

    def test_ambiguous_numbered_keys_are_dropped(self):
        assets = {"wv0033_0-5cm": {"href": "a"}, "wv1500_60-100cm": {"href": "b"}}
        others = [{"assets": {"wv0010_5-15cm": {}}}]
        renamed, _ = self.bc.numbered_keys(assets, others)
        self.assertEqual(renamed, {})

    def test_stable_keys_are_kept(self):
        assets = {"B04": {"href": "a"}, "vv": {"href": "b"}}
        others = [{"assets": {"B04": {}, "hh": {}}}]
        renamed, patterns = self.bc.numbered_keys(assets, others)
        self.assertEqual(set(renamed), {"B04", "vv"})
        self.assertEqual(patterns, {})

    def test_new_providers_in_catalogue(self):
        providers = {c.provider for c in COLLECTIONS}
        for key in ("thuenen", "lgln", "eodc", "paituli", "canada", "dlr"):
            self.assertIn(key, providers)


class SelectionTest(unittest.TestCase):
    def test_prefilter_by_area_and_period(self):
        from satellite_downloader.core import selection

        cologne = (6.9, 50.9, 7.0, 51.0)
        radar = [c for c in COLLECTIONS if c.category == "Radar"]
        keep, skipped = selection.prefilter(radar, cologne, "2026-08-01", "2026-09-15")
        keys = {c.key for c in keep}
        self.assertIn("s1-rtc", keys)
        self.assertNotIn("deafrica:s1_rtc", keys)  # Africa only
        reasons = {c.key: r for c, r in skipped}
        self.assertEqual(reasons.get("pc:alos-palsar-mosaic"), "no data in the period")

    def test_period_overlap(self):
        from satellite_downloader.core import selection

        mss = COLLECTIONS_BY_KEY["pc:landsat-c2-l1"]  # 1972-2013
        self.assertTrue(selection.overlaps_period(mss, "2000-01-01", "2001-01-01"))
        self.assertFalse(selection.overlaps_period(mss, "2020-01-01", "2021-01-01"))
        self.assertTrue(
            selection.overlaps_period(
                COLLECTIONS_BY_KEY["cop-dem-30"], "1900-01-01", "1901-01-01"
            )
        )

    def test_tag_gives_unique_ids_across_providers(self):
        from satellite_downloader.core import selection

        a = selection.tag({"id": "x"}, COLLECTIONS_BY_KEY["s2-l2a"])
        b = selection.tag(
            {"id": "x"}, COLLECTIONS_BY_KEY["earthsearch:sentinel-2-c1-l2a"]
        )
        self.assertNotEqual(a[selection.UID_KEY], b[selection.UID_KEY])
        self.assertEqual(selection.item_collection(b).provider, "earthsearch")


class SelectionRulesTest(unittest.TestCase):
    """Edge cases of the pre-search filters and the "nothing found" hint."""

    def setUp(self) -> None:
        from satellite_downloader.core import selection

        self.sel = selection
        self.s2 = COLLECTIONS_BY_KEY["s2-l2a"]  # 2015-06-27 to today, cloud cover
        self.dem = COLLECTIONS_BY_KEY["cop-dem-30"]  # static, no cloud cover

    def with_bbox(self, bbox) -> catalog.Collection:
        return dataclasses.replace(self.s2, bbox=bbox)

    def test_touching_extent_counts_as_covered(self):
        c = self.with_bbox((5.0, 45.0, 10.0, 48.0))
        for area in (
            (0.0, 46.0, 5.0, 47.0),  # touches west
            (10.0, 46.0, 12.0, 47.0),  # touches east
            (6.0, 40.0, 7.0, 45.0),  # touches south
            (6.0, 48.0, 7.0, 50.0),  # touches north
        ):
            with self.subTest(area):
                self.assertTrue(self.sel.covers(c, area))
        self.assertFalse(self.sel.covers(c, (10.1, 46.0, 12.0, 47.0)))
        self.assertFalse(self.sel.covers(c, (6.0, 48.1, 7.0, 50.0)))

    def test_swapped_south_and_north_still_cover(self):
        c = self.with_bbox((-180.0, 85.0, 180.0, -56.0))  # as ALOS PALSAR on PC
        self.assertTrue(self.sel.covers(c, (6.9, 50.9, 7.0, 51.0)))
        self.assertFalse(self.sel.covers(c, (6.9, -70.0, 7.0, -60.0)))

    def test_unknown_extent_covers_everything(self):
        self.assertTrue(self.sel.covers(self.with_bbox(None), (0, 0, 1, 1)))
        self.assertTrue(self.sel.covers(self.with_bbox((1.0, 2.0, 3.0)), (9, 9, 9, 9)))
        self.assertTrue(self.sel.covers(self.s2, None))

    def test_period_includes_first_and_last_day(self):
        c = dataclasses.replace(
            self.s2, interval=("2015-06-27T00:00:00Z", "2020-12-31T23:59:59Z")
        )
        self.assertTrue(self.sel.overlaps_period(c, "2020-12-31", "2021-03-01"))
        self.assertFalse(self.sel.overlaps_period(c, "2021-01-01", "2021-03-01"))
        self.assertTrue(self.sel.overlaps_period(c, "2015-01-01", "2015-06-27"))
        self.assertFalse(self.sel.overlaps_period(c, "2015-01-01", "2015-06-26"))

    def test_limited_reports_collections_at_the_limit(self):
        outcome = self.sel.SearchOutcome(counts={"a": 50, "b": 49, "c": 51})
        self.assertEqual(self.sel.limited(outcome, 50), ["a", "c"])

    def test_hint_for_a_period_without_data(self):
        hint = self.sel.no_results_hint([self.s2], "2000-01-01", "2001-01-01", None)
        self.assertEqual(
            hint, "No scenes: data exists from 2015-06-27 to today. Adjust the period."
        )

    def test_hint_lists_what_to_try(self):
        hint = self.sel.no_results_hint([self.s2], "2024-01-01", "2024-02-01", 20)
        self.assertEqual(
            hint,
            "No scenes found. You could widen the period, raise the maximum cloud "
            "cover (20 %) or choose another area.",
        )
        no_cloud_limit = self.sel.no_results_hint(
            [self.s2], "2024-01-01", "2024-02-01", 100
        )
        self.assertEqual(
            no_cloud_limit,
            "No scenes found. You could widen the period or choose another area.",
        )
        static = self.sel.no_results_hint([self.dem], "2024-01-01", "2024-02-01", 20)
        self.assertEqual(static, "No scenes found. You could choose another area.")


class PickerHelpersTest(unittest.TestCase):
    def test_filters(self):
        cbers = COLLECTIONS_BY_KEY["cb4a-wfi-sr"]
        self.assertTrue(matches(cbers, PROVIDERS["bdc"], ["cbers", "wfi"]))
        self.assertFalse(matches(cbers, PROVIDERS["bdc"], ["modis"]))
        naip = COLLECTIONS_BY_KEY["pc:naip"]
        self.assertFalse(covers(naip, (6.9, 50.9, 7.0, 51.0)))  # USA only, not Cologne
        self.assertTrue(covers(naip, (-74.0, 40.7, -73.9, 40.8)))
        self.assertEqual(period(COLLECTIONS_BY_KEY["cop-dem-30"]), "static")


if __name__ == "__main__":
    unittest.main()
