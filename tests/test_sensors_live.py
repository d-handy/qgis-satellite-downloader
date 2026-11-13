"""One real download per sensor family and provider. Run with SD_LIVE=1."""

import json
import os
import shutil
import unittest

import qgis_app

qgis_app.start()

from osgeo import gdal
from qgis.core import QgsFeedback

from satellite_downloader.core import places, stac
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY as C
from satellite_downloader.core.catalog import PROVIDERS as P
from satellite_downloader.core.pipeline import Downloader, DownloadJob

gdal.UseExceptions()

COLOGNE = (6.95, 50.94)
BRASILIA = (-47.85, -15.78)
NAIROBI = (36.82, -1.29)
ZURICH = (8.54, 47.37)
NEW_YORK = (-73.97, 40.78)
BRAUNSCHWEIG = (10.52, 52.27)
HANNOVER = (9.73, 52.37)
VILLACH = (13.85, 46.61)
VIENNA = (16.37, 48.21)
HELSINKI = (24.94, 60.17)
OTTAWA = (-75.70, 45.42)
LONDON = (-0.13, 51.51)
PODGORICA = (19.26, 42.44)
MANAUS = (-60.02, -3.12)
LONGYEARBYEN = (15.63, 78.22)
MCMURDO = (166.67, -77.85)

# key, place, radius km, date range, assets (None = defaults), minimum band count
CASES = [
    ("pc:hls2-s30", COLOGNE, 1, ("2026-06-01", "2026-09-15"), None, 3),
    (
        "pc:modis-13Q1-061",
        COLOGNE,
        3,
        ("2026-07-01", "2026-09-15"),
        ["250m_16_days_NDVI"],
        1,
    ),
    ("pc:aster-l1t", COLOGNE, 1, ("2000-03-01", "2006-12-31"), ["VNIR"], 3),
    ("pc:alos-palsar-mosaic", COLOGNE, 1, ("2015-01-01", "2021-12-31"), ["HH"], 1),
    ("pc:esa-worldcover", COLOGNE, 1, ("2021-01-01", "2021-12-31"), None, 1),
    ("pc:naip", NEW_YORK, 0.3, ("2021-01-01", "2023-12-31"), None, 4),
    ("pc:landsat-c2-l1", COLOGNE, 2, ("1975-01-01", "1980-12-31"), None, 3),
    (
        "pc:goes-cmi",
        BRASILIA,
        20,
        ("2026-09-01", "2026-09-02"),
        None,
        1,
    ),  # Americas only
    ("bdc:CB4-WFI-L4-SR-1", BRASILIA, 2, ("2026-06-01", "2026-09-15"), None, 3),
    ("bdc:AMZ1-WFI-L4-SR-1", BRASILIA, 2, ("2026-01-01", "2026-09-15"), None, 3),
    (
        "bdc:RESOURCESAT1_LISS3_L2_DN-1",
        BRASILIA,
        2,
        ("2009-01-01", "2013-12-31"),
        None,
        3,
    ),
    (
        "earthsearch:sentinel-2-c1-l2a",
        COLOGNE,
        1,
        ("2026-08-01", "2026-09-15"),
        None,
        3,
    ),
    ("deafrica:s2_l2a", NAIROBI, 1, ("2026-06-01", "2026-09-15"), None, 3),
    ("deafrica:ls9_sr", NAIROBI, 1, ("2026-01-01", "2026-09-15"), None, 3),
    ("deafrica:gm_s2_annual", NAIROBI, 1, ("2023-01-01", "2024-12-31"), None, 3),
    (
        "deafrica:rainfall_chirps_monthly",
        NAIROBI,
        20,
        ("2025-01-01", "2025-03-31"),
        None,
        1,
    ),
    (
        "swisstopo:ch.swisstopo.swissalti3d",
        ZURICH,
        0.2,
        ("2000-01-01", "2026-12-31"),
        None,
        1,
    ),
    (
        "swisstopo:ch.swisstopo.swissimage-dop10",
        ZURICH,
        0.05,
        ("2000-01-01", "2026-12-31"),
        None,
        3,
    ),
    (
        "swisstopo:ch.swisstopo.swisseo_s2-sr_v200",
        ZURICH,
        1,
        ("2026-08-01", "2026-09-15"),
        None,
        1,
    ),
    # further STAC APIs
    (
        "thuenen:crop-type-map-latest",
        BRAUNSCHWEIG,
        1,
        ("2017-01-01", "2026-12-31"),
        None,
        1,
    ),
    ("lgln:DOP", HANNOVER, 0.05, ("2012-01-01", "2026-12-31"), None, 3),
    (
        "kagis:KAGIS_coll_ortho_villach_2022160",
        VILLACH,
        0.05,
        ("2022-01-01", "2022-12-31"),
        None,
        3,
    ),
    ("eodc:AUT_DEM", VIENNA, 0.5, ("2018-01-01", "2026-12-31"), None, 1),
    (
        "paituli:syke_sentinel2_reflectance_mosaics_2022_onwards_at_fmi",
        HELSINKI,
        1,
        ("2022-01-01", "2025-12-31"),
        None,
        3,
    ),
    (
        "canada:canada-composite-landsat-2010",
        OTTAWA,
        2,
        ("2010-01-01", "2010-12-31"),
        None,
        1,
    ),
    (
        "ceda:clms-lcm-global-10m-yearly-v1-cog",
        LONDON,
        1,
        ("2020-01-01", "2020-12-31"),
        None,
        1,
    ),
    ("worldpop:MNE", PODGORICA, 10, ("2015-01-01", "2030-12-31"), None, 1),
    (
        "earthgenome:sentinel2-yearly-mosaics-v7",
        MANAUS,
        1,
        ("2025-01-01", "2026-12-31"),
        None,
        3,
    ),
    # static catalogues (bundled index)
    (
        "pgc:arcticdem-mosaics-v4.1-32m",
        LONGYEARBYEN,
        2,
        ("2000-01-01", "2030-01-01"),
        None,
        1,
    ),
    ("pgc:rema-mosaics-v2.0-32m", MCMURDO, 2, ("2000-01-01", "2030-01-01"), None, 1),
    ("glofas:depth-rp100", COLOGNE, 3, ("2000-01-01", "2030-01-01"), None, 1),
]


@unittest.skipUnless(qgis_app.LIVE, "set SD_LIVE=1 for tests against the live services")
class SensorDownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(os.path.join(qgis_app.OUTPUT, "sensors"), ignore_errors=True)

    def test_downloads(self):
        for key, (lon, lat), radius, (start, end), assets, min_bands in CASES:
            with self.subTest(key):
                self.assertIn(key, C, "collection missing from the catalogue")
                c = C[key]
                wkt = places.circle(lon, lat, radius)
                aoi = json.loads(gdal.ogr.CreateGeometryFromWkt(wkt).ExportToJson())
                items, _ = stac.search(P[c.provider], c, aoi, start, end, None, 50)
                self.assertTrue(items, f"{key}: no scenes")
                # newest acquisition only, to keep the test small
                first = stac.item_datetime(items[0])[:10]
                group = [i for i in items if stac.item_datetime(i)[:10] == first][:4]
                out = os.path.join(qgis_app.OUTPUT, "sensors", key.replace(":", "_"))
                job = DownloadJob(
                    P[c.provider],
                    c,
                    group,
                    list(assets or c.default_assets),
                    out,
                    wkt,
                    True,
                    c.native_res,
                    True,
                )
                messages: list[str] = []
                d = Downloader(
                    job, QgsFeedback(), lambda m, _l, msgs=messages: msgs.append(m)
                )
                d.run()
                if c.mode == "raw":
                    self.assertTrue(d.result.outputs, messages)
                    continue
                self.assertTrue(d.result.outputs, f"{key}: {messages}")
                ds = gdal.Open(d.result.outputs[0])
                self.assertGreaterEqual(ds.RasterCount, min_bands)
                valid = ds.GetRasterBand(1).ComputeRasterMinMax(
                    False, can_return_none=True
                )
                self.assertIsNotNone(valid, f"{key}: no valid pixels")
                ds = None


if __name__ == "__main__":
    unittest.main()
