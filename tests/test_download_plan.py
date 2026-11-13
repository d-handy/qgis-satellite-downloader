"""Offline checks of how ticked scenes become download jobs."""

import os
import unittest

import qgis_app

qgis_app.start()

from satellite_downloader.core import selection
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY, MODE_RAW
from satellite_downloader.gui.download_plan import (
    LARGE_DOWNLOAD_BYTES,
    BandChoice,
    OutputSettings,
    plan_downloads,
)

S2 = COLLECTIONS_BY_KEY["s2-l2a"]
LANDSAT = COLLECTIONS_BY_KEY["landsat-c2-l2"]
RAW = next(c for c in COLLECTIONS_BY_KEY.values() if c.mode == MODE_RAW)


def item(collection, item_id: str, day: str) -> dict:
    tagged = selection.tag(
        {"id": item_id, "properties": {"datetime": f"{day}T10:00:00Z"}, "assets": {}},
        collection,
    )
    return tagged


def settings(**kw) -> OutputSettings:
    values = {
        "out_dir": os.path.join("out", "dir"),
        "aoi_wgs84_wkt": "POLYGON ((6.9 50.9, 7 50.9, 7 51, 6.9 51, 6.9 50.9))",
        "area_size_m": (1000.0, 2000.0),
        "clip_to_aoi": True,
        "mosaic_by_date": True,
        "dst_crs_wkt": None,
        "resampling": "bilinear",
        **kw,
    }
    return OutputSettings(**values)


class PlanDownloadsTest(unittest.TestCase):
    def test_one_job_per_dataset_with_its_defaults(self):
        items = [
            item(S2, "a", "2026-09-01"),
            item(LANDSAT, "b", "2026-09-02"),
            item(S2, "c", "2026-09-03"),
        ]
        plan = plan_downloads(items, settings(), None)
        self.assertEqual(
            [(j.collection.key, [i["id"] for i in j.items]) for j in plan.jobs],
            [("s2-l2a", ["a", "c"]), ("landsat-c2-l2", ["b"])],
        )
        s2_job = plan.jobs[0]
        self.assertEqual(s2_job.assets, list(S2.default_assets))
        self.assertEqual(s2_job.resolution_m, S2.native_res)
        self.assertEqual(s2_job.out_dir, os.path.join("out", "dir", "sentinel-2-l2a"))
        self.assertTrue(s2_job.clip_to_aoi)
        self.assertEqual(s2_job.resampling, "bilinear")

    def test_band_choice_applies_to_the_shown_dataset(self):
        plan = plan_downloads(
            [item(S2, "a", "2026-09-01")], settings(), BandChoice(["B08", "B04"], 20.0)
        )
        (job,) = plan.jobs
        self.assertEqual((job.assets, job.resolution_m), (["B08", "B04"], 20.0))

    def test_no_bands_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            plan_downloads(
                [item(S2, "a", "2026-09-01")], settings(), BandChoice([], 10)
            )
        self.assertIn("No bands ticked", str(ctx.exception))

    def test_estimate_counts_one_file_per_day_when_mosaicking(self):
        # 1000 × 2000 m at 10 m: 100 × 200 pixels, 4 bands, 4 bytes
        items = [
            item(S2, "a", "2026-09-01"),
            item(S2, "b", "2026-09-01"),  # same day, same mosaic
            item(S2, "c", "2026-09-05"),
        ]
        per_file = 100 * 200 * len(S2.default_assets) * 4
        mosaic = plan_downloads(items, settings(), None)
        self.assertEqual(mosaic.estimate, 2 * per_file)
        separate = plan_downloads(items, settings(mosaic_by_date=False), None)
        self.assertEqual(separate.estimate, 3 * per_file)

    def test_estimate_rounds_partial_pixels_up(self):
        plan = plan_downloads(
            [item(S2, "a", "2026-09-01")],
            settings(area_size_m=(1001.0, 10.0)),
            BandChoice(["B04"], 10.0),
        )
        self.assertEqual(plan.estimate, 101 * 1 * 4)

    def test_whole_file_downloads_are_counted_not_estimated(self):
        plan = plan_downloads(
            [item(RAW, "r1", "2026-09-01"), item(RAW, "r2", "2026-09-02")],
            settings(),
            None,
        )
        self.assertEqual((plan.raw_scenes, plan.estimate), (2, 0))
        self.assertIn("2 scenes are downloaded as whole files", plan.warning(2) or "")

    def test_large_estimate_asks_for_confirmation(self):
        huge = settings(area_size_m=(200_000.0, 200_000.0))
        plan = plan_downloads([item(S2, "a", "2026-09-01")], huge, None)
        self.assertGreater(plan.estimate, LARGE_DOWNLOAD_BYTES)
        self.assertIn("GB uncompressed", plan.warning(1) or "")
        small = plan_downloads([item(S2, "a", "2026-09-01")], settings(), None)
        self.assertIsNone(small.warning(1))


if __name__ == "__main__":
    unittest.main()
