"""Tests against the live catalogues. Run with SD_LIVE=1 (needs internet)."""

import glob
import json
import os
import shutil
import unittest
import urllib.request

import qgis_app

qgis_app.start()

import numpy as np
from osgeo import gdal
from qgis.core import QgsFeedback

from satellite_downloader.core import clouds, geometry, stac
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY as C
from satellite_downloader.core.catalog import PROVIDERS as P
from satellite_downloader.core.pipeline import (
    GDAL_HTTP_OPTIONS,
    Downloader,
    DownloadJob,
)

gdal.UseExceptions()

COLOGNE = "POLYGON((6.93 50.93, 6.97 50.93, 6.97 50.96, 6.93 50.96, 6.93 50.93))"
TRIANGLE = "POLYGON((6.93 50.93, 6.97 50.93, 6.95 50.96, 6.93 50.93))"
BRASILIA = (
    "POLYGON((-47.95 -15.85, -47.75 -15.85, -47.75 -15.7, -47.95 -15.7, -47.95 -15.85))"
)
ATACAMA_USER = "POLYGON((-70.64 -24.59, -70.06 -24.59, -70.06 -24.32, -70.64 -24.32, -70.64 -24.59))"
# Rectangle across the antimeridian as it arrives from a projected CRS
FIJI = "POLYGON((179.9 -16.85, -179.9 -16.85, -179.9 -16.7, 179.9 -16.7, 179.9 -16.85))"
SVALBARD = "POLYGON((15.5 78.2, 15.7 78.2, 15.7 78.25, 15.5 78.25, 15.5 78.2))"


def gj(wkt):
    return json.loads(geometry.ogr.CreateGeometryFromWkt(wkt).ExportToJson())


def search(key, wkt, start, end, cloud=None, n=5):
    c = C[key]
    return stac.search(P[c.provider], c, gj(wkt), start, end, cloud, n)


def run(key, wkt, items, assets=None, polygon=False, dst=None, mosaic=True, sub=None):
    c = C[key]
    out = os.path.join(qgis_app.OUTPUT, sub or c.collection_id)
    dst_wkt = geometry.srs(dst).ExportToWkt() if dst else None
    job = DownloadJob(
        P[c.provider],
        c,
        items,
        list(assets or c.default_assets),
        out,
        wkt,
        polygon,
        c.native_res,
        mosaic,
        dst_wkt,
    )
    messages = []
    downloader = Downloader(job, QgsFeedback(), lambda m, _l: messages.append(m))
    downloader.run()
    return downloader, messages


def same_as_source(path, item, asset, provider):
    """Output pixels equal the source pixels at the same coordinates."""
    out = gdal.Open(path)
    gt = out.GetGeoTransform()
    signer = stac.signer_for(P[provider])
    with gdal.config_options(GDAL_HTTP_OPTIONS):
        src = gdal.Open("/vsicurl/" + signer.sign(item["assets"][asset]["href"]))
        sgt = src.GetGeoTransform()
        ox = (gt[0] - sgt[0]) / sgt[1]
        oy = (gt[3] - sgt[3]) / sgt[5]
        if (
            abs(ox - round(ox)) > 1e-6
            or abs(oy - round(oy)) > 1e-6
            or not src.GetSpatialRef().IsSame(out.GetSpatialRef())
        ):
            return False
        arr = out.GetRasterBand(1).ReadAsArray()
        sarr = src.GetRasterBand(1).ReadAsArray(
            round(ox), round(oy), out.RasterXSize, out.RasterYSize
        )
    nd = out.GetRasterBand(1).GetNoDataValue()
    valid = arr != nd if nd is not None else np.ones(arr.shape, bool)
    return bool(valid.any() and (arr[valid] == sarr[valid]).all())


@unittest.skipUnless(qgis_app.LIVE, "set SD_LIVE=1 for tests against the live services")
class LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(qgis_app.OUTPUT, ignore_errors=True)

    # -- regression: CBERS scenes outside their footprint (user report) -------

    def test_cbers_bbox_only_match_is_discarded(self):
        items, dropped = search(
            "cb4a-wpm-dn", ATACAMA_USER, "2026-08-23", "2026-08-23", n=20
        )
        self.assertNotIn("CBERS_4A_WPM_20260823_231_144_L4", [i["id"] for i in items])
        self.assertGreaterEqual(dropped, 1)

    def test_forced_non_overlapping_scene_writes_nothing(self):
        url = (
            "https://data.inpe.br/bdc/stac/v1/collections/CB4A-WPM-L4-DN-1/items/"
            "CBERS_4A_WPM_20260823_231_144_L4"
        )
        bad = json.load(urllib.request.urlopen(url))
        d, _ = run("cb4a-wpm-dn", ATACAMA_USER, [bad], sub="wpm-empty")
        self.assertEqual(d.result.empty, 1)
        self.assertEqual(glob.glob(os.path.join(d.job.out_dir, "*.tif")), [])

    def test_cbers_stack_nodata_crs_and_pixels(self):
        items, _ = search("cb4a-wfi-sr", BRASILIA, "2026-08-01", "2026-09-15", 20, 1)
        d, _ = run(
            "cb4a-wfi-sr",
            BRASILIA,
            items,
            assets=["BAND13", "BAND14", "BAND15", "BAND16", "CMASK"],
        )
        ds = gdal.Open(d.result.outputs[0])
        self.assertEqual(
            {ds.GetRasterBand(i).GetNoDataValue() for i in range(1, 6)}, {-9999.0}
        )
        cmask = ds.GetRasterBand(5).ReadAsArray()
        self.assertFalse((cmask == 0).any())
        self.assertEqual(ds.GetSpatialRef().GetAuthorityCode(None), "32722")
        self.assertTrue(same_as_source(d.result.outputs[0], items[0], "BAND13", "bdc"))

    # -- pixel grid -------------------------------------------------------------

    def test_landsat_pixels_identical_and_reuse_by_hash(self):
        items, _ = search("landsat-c2-l2", COLOGNE, "2026-07-01", "2026-09-15", 30, 1)
        d, _ = run("landsat-c2-l2", COLOGNE, items, assets=["red"])
        self.assertTrue(same_as_source(d.result.outputs[0], items[0], "red", "pc"))
        again, messages = run("landsat-c2-l2", COLOGNE, items, assets=["red"])
        self.assertEqual(again.result.outputs, d.result.outputs)
        self.assertTrue(any("skipped" in m for m in messages))
        other, _ = run("landsat-c2-l2", COLOGNE, items, assets=["red", "nir08"])
        self.assertNotEqual(other.result.outputs, d.result.outputs)

    def test_explicit_crs_respected(self):
        items, _ = search("cb4a-wfi-sr", BRASILIA, "2026-08-01", "2026-09-15", 20, 1)
        d, _ = run(
            "cb4a-wfi-sr",
            BRASILIA,
            items,
            assets=["BAND13"],
            dst="EPSG:32723",
            sub="wfi-32723",
        )
        self.assertEqual(
            gdal.Open(d.result.outputs[0]).GetSpatialRef().GetAuthorityCode(None),
            "32723",
        )

    def test_dem_geographic_source_goes_to_utm(self):
        items, _ = search("cop-dem-30", COLOGNE, "2026-01-01", "2026-01-02")
        d, _ = run("cop-dem-30", COLOGNE, items)
        ds = gdal.Open(d.result.outputs[0])
        self.assertEqual(ds.GetSpatialRef().GetAuthorityCode(None), "32632")
        self.assertIsNotNone(
            ds.GetRasterBand(1).ComputeRasterMinMax(False, can_return_none=True)
        )

    # -- new: orbits, anywhere on Earth -------------------------------------------

    def test_sentinel1_orbits_not_mixed(self):
        items, _ = search("s1-rtc", COLOGNE, "2026-08-15", "2026-09-15", n=30)
        states = {i["properties"]["sat:orbit_state"] for i in items}
        self.assertEqual(states, {"ascending", "descending"})
        d, _ = run("s1-rtc", COLOGNE, items[:6], assets=["vv"])
        names = [os.path.basename(p) for p in d.result.outputs]
        self.assertTrue(
            names and all("_asc_" in n or "_desc_" in n for n in names), names
        )
        for path in d.result.outputs:
            with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as fh:
                used = set(json.load(fh)["items"])
            orbit = {
                i["properties"]["sat:orbit_state"] for i in items if i["id"] in used
            }
            self.assertEqual(len(orbit), 1, path)

    def test_fiji_across_antimeridian(self):
        items, _ = search("s2-l2a", FIJI, "2026-06-01", "2026-09-15", 30, 10)
        self.assertTrue(items)
        d, messages = run("s2-l2a", FIJI, items[:2], assets=["B04"], sub="fiji")
        self.assertTrue(d.result.outputs, messages)
        ds = gdal.Open(d.result.outputs[0])
        # About 22 km wide: a wrong antimeridian handling produces a globe-sized raster.
        self.assertLess(ds.RasterXSize * 10, 40_000)
        self.assertIsNotNone(
            ds.GetRasterBand(1).ComputeRasterMinMax(False, can_return_none=True)
        )

    def test_svalbard(self):
        items, _ = search("s1-rtc", SVALBARD, "2026-08-01", "2026-09-15", n=3)
        self.assertTrue(items)
        # Polar areas are often acquired in HH/HV only.
        pol = next(p for p in ("vv", "hh") if p in items[0]["assets"])
        d, messages = run("s1-rtc", SVALBARD, items[:1], assets=[pol], sub="svalbard")
        self.assertTrue(d.result.outputs, messages)

    def test_missing_band_gives_clear_message(self):
        items, _ = search("s1-rtc", SVALBARD, "2026-08-01", "2026-09-15", n=3)
        absent = next(
            p for p in ("vv", "hh") if all(p not in i["assets"] for i in items)
        )
        with self.assertRaisesRegex(ValueError, "None of the scenes contains"):
            run("s1-rtc", SVALBARD, items, assets=[absent], sub="svalbard-missing")

    # -- new: clear sky inside the AOI ---------------------------------------------

    def test_clear_percent_matches_full_resolution_mask(self):
        items, _ = search("s2-l2a", COLOGNE, "2026-06-01", "2026-09-15", 80, 6)
        aoi = geometry.from_geojson(gj(COLOGNE))
        clouds.estimate(
            items, C["s2-l2a"], aoi, stac.signer_for(P["pc"]), GDAL_HTTP_OPTIONS
        )
        values = [i[stac.CLEAR_KEY] for i in items]
        self.assertTrue(all(v is not None and 0 <= v <= 100 for v in values), values)
        # Reference: the SCL band downloaded at native 20 m resolution for the AOI.
        item = items[0]
        d, _ = run(
            "s2-l2a", COLOGNE, [item], assets=["SCL"], polygon=True, sub="scl-ref"
        )
        ds = gdal.Open(d.result.outputs[0])
        scl = ds.GetRasterBand(1).ReadAsArray().astype(np.int64)
        scl[scl == 0] = -1  # outside the AOI (nodata) is not counted
        clear, cloudy = clouds.classify(scl, C["s2-l2a"].mask)
        reference = 100 * clear / (clear + cloudy)
        self.assertAlmostEqual(item[stac.CLEAR_KEY], reference, delta=5)

    # -- new: resumable whole-file download ------------------------------------------

    def test_raw_download_resumes_part_file(self):
        items, _ = search("s3-slstr-lst", COLOGNE, "2026-09-01", "2026-09-05", n=1)
        key = "safe-manifest"
        c = C["s3-slstr-lst"]
        full, _ = run("s3-slstr-lst", COLOGNE, items, assets=[key], sub="s3-full")
        with open(full.result.outputs[0], "rb") as fh:
            expected = fh.read()
        target_dir = os.path.join(qgis_app.OUTPUT, "s3-resume", items[0]["id"])
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(full.result.outputs[0]))
        with open(target + ".part", "wb") as fh:
            fh.write(expected[: len(expected) // 2])
        resumed, _ = run("s3-slstr-lst", COLOGNE, items, assets=[key], sub="s3-resume")
        with open(resumed.result.outputs[0], "rb") as fh:
            self.assertEqual(fh.read(), expected)
        self.assertEqual(c.mode, "raw")


if __name__ == "__main__":
    unittest.main()
