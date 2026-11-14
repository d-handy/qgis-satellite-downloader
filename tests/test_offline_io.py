"""Offline tests for code that reads rasters over HTTP or writes derived files.

The cloud estimate and whole-file downloads go through GDAL's /vsicurl/, so a
local HTTP server (``local_http``) stands in for the catalogue's storage. The
recipe planner gets a fake catalogue search; no test touches the network.
"""

import dataclasses
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import qgis_app

qgis_app.start()

import numpy as np
from local_http import LocalServer
from osgeo import gdal, ogr, osr
from qgis.core import QgsFeedback

from satellite_downloader.core import clouds, geometry, postprocess, recipes, stac
from satellite_downloader.core.catalog import (
    COLLECTIONS_BY_KEY,
    MODE_RAW,
    PROVIDERS,
)
from satellite_downloader.core.pipeline import Downloader, DownloadJob

gdal.UseExceptions()

_UTM32 = "EPSG:32632"
_ORIGIN = (500_000.0, 5_600_000.0)


def _write(
    path: str,
    values: np.ndarray,
    res: float,
    data_type: int = gdal.GDT_Byte,
    nodata: float | None = None,
) -> None:
    ds = gdal.GetDriverByName("GTiff").Create(
        path, values.shape[1], values.shape[0], 1, data_type, ["TILED=YES"]
    )
    ds.SetGeoTransform((_ORIGIN[0], res, 0, _ORIGIN[1], 0, -res))
    ds.SetProjection(geometry.srs(_UTM32).ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(values)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    ds = None


def _some(value: float | None) -> float:
    """The value, or a test failure if the code under test returned None."""
    if value is None:
        raise AssertionError("expected a value, got None")
    return value


def _read(path: str) -> np.ndarray:
    ds = gdal.Open(path)
    arr = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return arr


def _utm_box_wgs84(xmin: float, ymin: float, xmax: float, ymax: float) -> ogr.Geometry:
    """Rectangle given in UTM 32N, returned densified in EPSG:4326 (lon/lat)."""
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)):
        ring.AddPoint_2D(x, y)
    box = ogr.Geometry(ogr.wkbPolygon)
    box.AddGeometry(ring)
    box.Segmentize(100)
    box.Transform(
        osr.CoordinateTransformation(geometry.srs(_UTM32), geometry.srs("EPSG:4326"))
    )
    return box


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name


# -- clear sky over the AOI --------------------------------------------------------


class ClearPercentTest(_TempDir):
    """``clouds.clear_percent`` reads the real mask band through /vsicurl/."""

    size = 512  # pixels at 20 m: 10.24 km
    res = 20.0

    def scl(self, name: str, values: np.ndarray) -> str:
        _write(os.path.join(self.dir, name), values, self.res)
        return name

    def aoi(self) -> ogr.Geometry:
        span = self.size * self.res
        return _utm_box_wgs84(
            _ORIGIN[0] + 200,
            _ORIGIN[1] - span + 200,
            _ORIGIN[0] + span - 200,
            _ORIGIN[1] - 200,
        )

    def item(self, server: LocalServer, name: str) -> dict:
        return {
            "id": name,
            "properties": {},
            "assets": {"SCL": {"href": server.url(name)}},
        }

    def test_half_cloudy_area(self):
        values = np.full((self.size, self.size), 4, np.uint8)  # 4 vegetation
        values[:, self.size // 2 :] = 8  # 8 cloud, medium probability
        name = self.scl("half.tif", values)
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        with LocalServer(self.dir) as server:
            clear = clouds.clear_percent(
                self.item(server, name), s2, self.aoi(), stac.NullSigner()
            )
        self.assertAlmostEqual(_some(clear), 50.0, delta=2.0)

    def test_nodata_and_saturated_pixels_do_not_count(self):
        values = np.full((self.size, self.size), 4, np.uint8)
        values[: self.size // 2, :] = 0  # no data
        values[self.size // 2 :, : self.size // 4] = 1  # saturated: invalid
        values[self.size // 2 :, self.size // 4 : self.size // 2] = 9  # cloud
        name = self.scl("invalid.tif", values)
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        with LocalServer(self.dir) as server:
            clear = clouds.clear_percent(
                self.item(server, name), s2, self.aoi(), stac.NullSigner()
            )
        # Of the valid lower half: one third cloud (9), two thirds vegetation (4)
        self.assertAlmostEqual(_some(clear), 200 / 3, delta=2.0)

    def test_tiles_of_one_acquisition_form_one_mosaic(self):
        half = self.size // 2
        left = np.full((self.size, self.size), 0, np.uint8)
        left[:, :half] = 4
        right = np.full((self.size, self.size), 0, np.uint8)
        right[:, half:] = 8
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        names = [self.scl("left.tif", left), self.scl("right.tif", right)]
        with LocalServer(self.dir) as server:
            items = [self.item(server, n) for n in names]
            clear = clouds.clear_percent(items, s2, self.aoi(), stac.NullSigner())
        self.assertAlmostEqual(_some(clear), 50.0, delta=2.0)

    def test_undeterminable_cases_return_none(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        no_mask = dataclasses.replace(s2, mask=None)
        item = {"id": "x", "properties": {}, "assets": {"B04": {"href": "https://x"}}}
        self.assertIsNone(clouds.clear_percent(item, no_mask, self.aoi(), None))
        self.assertIsNone(clouds.clear_percent(item, s2, self.aoi(), None))  # no SCL
        only_nodata = self.scl("empty.tif", np.zeros((self.size, self.size), np.uint8))
        with LocalServer(self.dir) as server:
            self.assertIsNone(
                clouds.clear_percent(
                    self.item(server, only_nodata), s2, self.aoi(), stac.NullSigner()
                )
            )


# -- whole-file downloads ------------------------------------------------------------


class RawDownloadTest(_TempDir):
    """``Downloader._run_raw`` streams files in chunks and resumes ``.part`` files."""

    content = os.urandom(3 * 1024 * 1024 + 123)

    def setUp(self) -> None:
        super().setUp()
        self.served = os.path.join(self.dir, "served")
        os.makedirs(self.served)
        with open(os.path.join(self.served, "scene.nc"), "wb") as fh:
            fh.write(self.content)
        self.out = os.path.join(self.dir, "out")
        self.target = os.path.join(self.out, "S3_scene", "scene.nc")
        self.logs: list[str] = []

    def run_raw(self, server: LocalServer) -> list[str]:
        raw = dataclasses.replace(COLLECTIONS_BY_KEY["s2-l2a"], mode=MODE_RAW)
        item = {
            "id": "S3_scene",
            "properties": {"datetime": "2026-09-01T10:00:00Z"},
            "assets": {"data": {"href": server.url("scene.nc")}},
        }
        job = DownloadJob(
            PROVIDERS["earthsearch"],
            raw,
            [item],
            ["data"],
            self.out,
            "POINT (9 50.5)",
            False,
            300.0,
            False,
        )
        result = Downloader(job, QgsFeedback(), lambda m, _l: self.logs.append(m)).run()
        self.assertEqual(result.failures, 0, self.logs)
        return result.outputs

    def test_file_arrives_complete(self):
        with LocalServer(self.served) as server:
            outputs = self.run_raw(server)
        self.assertEqual(outputs, [self.target])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.content)
        self.assertFalse(os.path.exists(self.target + ".part"))

    def test_interrupted_download_is_resumed(self):
        os.makedirs(os.path.dirname(self.target))
        # A marker prefix proves the existing bytes were kept, not fetched again.
        with open(self.target + ".part", "wb") as fh:
            fh.write(b"X" * 1000)
        with LocalServer(self.served) as server:
            self.run_raw(server)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"X" * 1000 + self.content[1000:])

    def test_oversized_part_file_starts_over(self):
        os.makedirs(os.path.dirname(self.target))
        with open(self.target + ".part", "wb") as fh:
            fh.write(b"X" * (len(self.content) + 10))
        with LocalServer(self.served) as server:
            self.run_raw(server)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.content)

    def test_unreachable_file_is_a_failure_not_a_crash(self):
        """A missing file counts as one failure; the other files still arrive."""
        raw = dataclasses.replace(COLLECTIONS_BY_KEY["s2-l2a"], mode=MODE_RAW)
        with LocalServer(self.served) as server:
            items = [
                {
                    "id": item_id,
                    "properties": {"datetime": "2026-09-01T10:00:00Z"},
                    "assets": {"data": {"href": server.url(name)}},
                }
                for item_id, name in (("gone", "gone.nc"), ("S3_scene", "scene.nc"))
            ]
            job = DownloadJob(
                PROVIDERS["earthsearch"],
                raw,
                items,
                ["data"],
                self.out,
                "POINT (9 50.5)",
                False,
                300.0,
                False,
            )
            result = Downloader(
                job, QgsFeedback(), lambda m, _l: self.logs.append(m)
            ).run()
        self.assertEqual(result.failures, 1)
        self.assertEqual(result.outputs, [self.target])
        self.assertTrue(any("gone" in m and "failed" in m for m in self.logs))

    def test_existing_file_is_not_downloaded_again(self):
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "wb") as fh:
            fh.write(b"done")
        with LocalServer(self.served) as server:
            outputs = self.run_raw(server)
        self.assertEqual(outputs, [self.target])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"done")
        self.assertTrue(any("Already downloaded" in m for m in self.logs))


# -- derived products ----------------------------------------------------------------


class PostprocessTest(_TempDir):
    def test_db_conversion_masks_nodata_and_non_positive_values(self):
        src = os.path.join(self.dir, "sar.tif")
        values = np.array([[1, 10, 100, 0, 65535]], np.uint16)
        _write(src, values, 10.0, gdal.GDT_UInt16, nodata=65535)
        out = postprocess.to_db(src)
        self.assertEqual(out, os.path.join(self.dir, "sar_dB.tif"))
        ds = gdal.Open(out)
        band = ds.GetRasterBand(1)
        nd = postprocess.DB_NODATA
        np.testing.assert_allclose(band.ReadAsArray(), [[0, 10, 20, nd, nd]])
        self.assertEqual(band.GetNoDataValue(), nd)
        self.assertEqual(band.DataType, gdal.GDT_Float32)
        self.assertEqual(
            ds.GetGeoTransform(), (_ORIGIN[0], 10.0, 0, _ORIGIN[1], 0, -10.0)
        )
        self.assertTrue(ds.GetSpatialRef().IsSame(geometry.srs(_UTM32)))
        ds = None

    def test_db_conversion_reuses_an_existing_result(self):
        src = os.path.join(self.dir, "sar.tif")
        _write(src, np.ones((4, 4), np.uint16), 10.0, gdal.GDT_UInt16)
        first = postprocess.to_db(src)
        mtime = os.path.getmtime(first)
        self.assertEqual(postprocess.to_db(src), first)
        self.assertEqual(os.path.getmtime(first), mtime)

    def test_slope_of_a_45_degree_plane(self):
        # Height rises 10 m per 10 m pixel eastwards: 45° everywhere.
        dem = os.path.join(self.dir, "dem.tif")
        cols = np.arange(64, dtype=np.float32) * 10.0
        _write(dem, np.tile(cols, (64, 1)), 10.0, gdal.GDT_Float32)
        slope = _read(postprocess.slope(dem))
        # computeEdges extrapolates; only the four corner pixels deviate
        np.testing.assert_allclose(slope[1:-1, :], 45.0, atol=0.01)
        np.testing.assert_allclose(slope[:, 1:-1], 45.0, atol=0.01)

    def test_hillshade_of_a_plane_is_uniform(self):
        dem = os.path.join(self.dir, "dem.tif")
        cols = np.arange(64, dtype=np.float32) * 10.0
        _write(dem, np.tile(cols, (64, 1)), 10.0, gdal.GDT_Float32)
        path = postprocess.hillshade(dem)
        self.assertTrue(path.endswith("dem_hillshade.tif"))
        shade = _read(path)[1:-1, 1:-1]
        self.assertEqual(shade.min(), shade.max())
        self.assertGreater(int(shade.min()), 0)


# -- recipe planning -------------------------------------------------------------------

_COLOGNE = "POLYGON ((6.90 50.90, 7.00 50.90, 7.00 50.97, 6.90 50.97, 6.90 50.90))"
_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[6, 50], [8, 50], [8, 52], [6, 52], [6, 50]]],
}


def _s2_item(item_id: str, day: str, orbit: int = 8) -> dict:
    return {
        "id": item_id,
        "geometry": _FOOTPRINT,
        "properties": {"datetime": f"{day}T10:30:00Z", "sat:relative_orbit": orbit},
        "assets": {
            b: {"href": f"https://example.org/{item_id}/{b}.tif"}
            for b in ("B02", "B03", "B04", "SCL")
        },
    }


class RecipePlanTest(unittest.TestCase):
    """``recipes.plan`` with a fake catalogue and fake cloud estimates."""

    today = date(2026, 9, 20)

    def plan(
        self, recipe_key: str, items: dict[str, list[dict]], clear: dict[str, float]
    ):
        def search(provider, collection, *_args, **_kw):
            return items.get(collection.key, []), None

        def clear_percent(acq_items, *_args, **_kw):
            return clear.get(acq_items[0]["id"])

        with (
            mock.patch.object(recipes.stac, "search", side_effect=search),
            mock.patch.object(
                recipes.clouds, "clear_percent", side_effect=clear_percent
            ),
        ):
            return recipes.plan(
                recipes.RECIPES_BY_KEY[recipe_key], _COLOGNE, self.today
            )

    def test_newest_acquisition_that_is_clear_enough_wins(self):
        items = {
            "s2-l2a": [
                _s2_item("cloudy_new", "2026-09-18"),
                _s2_item("clear_old", "2026-09-10"),
                _s2_item("clear_older", "2026-09-02"),
            ]
        }
        clear = {"cloudy_new": 20.0, "clear_old": 99.0, "clear_older": 100.0}
        result = self.plan("current", items, clear)
        self.assertEqual(result.acquisition.items[0]["id"], "clear_old")
        self.assertEqual(result.assets, ["B04", "B03", "B02"])
        self.assertEqual(result.resolution_m, 10.0)
        self.assertEqual(result.notes, [])

    def test_clearest_is_used_with_a_note_when_none_is_clear_enough(self):
        items = {"s2-l2a": [_s2_item("a", "2026-09-18"), _s2_item("b", "2026-09-10")]}
        result = self.plan("current", items, {"a": 30.0, "b": 70.0})
        self.assertEqual(result.acquisition.items[0]["id"], "b")
        self.assertEqual(len(result.notes), 1)
        self.assertIn("70 % clear", result.notes[0])

    def test_nothing_found_explains_why(self):
        with self.assertRaises(recipes.NoDataError) as ctx:
            self.plan("current", {}, {})
        self.assertIn("Sentinel-2", str(ctx.exception))

    def plan_with_failing(
        self,
        failing: set[str],
        items: dict[str, list[dict]],
        calls: list[str] | None = None,
    ):
        """``plan`` where the sources in ``failing`` raise like an unreachable API.

        Every searched collection key is appended to ``calls``.
        """
        calls = [] if calls is None else calls

        def search(provider, collection, *_args, **_kw):
            calls.append(collection.key)
            if collection.key in failing:
                raise stac.StacError(f"{collection.key}: 503 Service Unavailable", 503)
            return items.get(collection.key, []), 0

        with (
            mock.patch.object(recipes.stac, "search", side_effect=search),
            mock.patch.object(recipes.clouds, "clear_percent", return_value=99.0),
        ):
            result = recipes.plan(
                recipes.RECIPES_BY_KEY["current"], _COLOGNE, self.today
            )
        return result, calls

    def test_one_unreachable_source_does_not_stop_the_recipe(self):
        result, _ = self.plan_with_failing(
            {"landsat-c2-l2"}, {"s2-l2a": [_s2_item("s2", "2026-09-18")]}
        )
        self.assertEqual(result.acquisition.items[0]["id"], "s2")
        self.assertEqual(len(result.notes), 1)
        self.assertIn("Landsat", result.notes[0])
        self.assertIn("could not be reached", result.notes[0])

    def test_unreachable_source_is_not_asked_again_in_wider_windows(self):
        # Sentinel-2 finds nothing, so all three time windows are searched.
        calls: list[str] = []
        with self.assertRaises(recipes.NoDataError) as ctx:
            self.plan_with_failing({"landsat-c2-l2"}, {}, calls)
        self.assertEqual(calls.count("s2-l2a"), 3)
        self.assertEqual(calls.count("landsat-c2-l2"), 1)
        # The explanation for "nothing found" mentions the missing source.
        self.assertIn("could not be reached", str(ctx.exception))

    def test_all_sources_unreachable_says_so(self):
        with self.assertRaises(recipes.NoDataError) as ctx:
            self.plan_with_failing({"s2-l2a", "landsat-c2-l2"}, {})
        message = str(ctx.exception)
        self.assertIn("Could not reach", message)
        self.assertIn("Try again later", message)
        self.assertNotIn("open sea", message)  # not the "nothing here" explanation

    def test_one_signer_per_provider_for_the_whole_plan(self):
        """Each signer caches its own tokens; one per acquisition asks again each time."""
        items = {"s2-l2a": [_s2_item(f"s{n}", f"2026-09-{10 + n}") for n in range(6)]}
        with mock.patch.object(
            recipes.stac, "signer_for", wraps=stac.signer_for
        ) as signer_for:
            self.plan("current", items, {f"s{n}": 20.0 for n in range(6)})
        self.assertEqual(signer_for.call_count, 1)

    def test_partial_coverage_is_reported(self):
        item = _s2_item("edge", "2026-09-18")
        item["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[6.95, 50], [8, 50], [8, 52], [6.95, 52], [6.95, 50]]],
        }
        result = self.plan(
            "radar", {"s1-rtc": [{**item, "assets": {"vv": {}, "vh": {}}}]}, {}
        )
        self.assertEqual(len(result.notes), 1)
        self.assertIn("No acquisition covers the whole area", result.notes[0])
        self.assertAlmostEqual(_some(result.acquisition.coverage), 50.0, delta=1.0)

    def test_large_area_coarsens_resolution(self):
        big = "POLYGON ((4 49, 10 49, 10 53, 4 53, 4 49))"  # about 440 km tall
        tile = {
            "id": "dem",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[3, 48], [11, 48], [11, 54], [3, 54], [3, 48]]],
            },
            "properties": {"datetime": "2021-04-22T00:00:00Z"},
            "assets": {"data": {}},
        }

        def search(provider, collection, *_args, **_kw):
            return [tile], None

        with mock.patch.object(recipes.stac, "search", side_effect=search):
            result = recipes.plan(recipes.RECIPES_BY_KEY["dem"], big, self.today)
        self.assertGreater(result.resolution_m, 30.0)
        self.assertEqual(result.resolution_m % 30.0, 0.0)  # stays on the native grid
        self.assertIn("resolution automatically coarsened", result.notes[0])


class AttributionTest(unittest.TestCase):
    """Source and licence line written into the project for each result."""

    info = {
        "providers": [{"name": "ESA"}, {"name": "Microsoft"}, {"name": "ESA"}, {}],
        "license": "proprietary",
        "links": [
            {"rel": "self", "href": "https://x/self"},
            {"rel": "license", "href": "https://x/licence"},
        ],
    }

    def setUp(self) -> None:
        recipes._collection_info.clear()
        self.addCleanup(recipes._collection_info.clear)

    def test_sentinel_source_and_licence(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        with mock.patch.object(stac, "get_json", return_value=self.info) as get:
            text = recipes.attribution(s2, "2026-09-18T10:30:00Z")
            recipes.attribution(s2, "2026-09-18T10:30:00Z")
        self.assertEqual(
            text,
            "Contains modified Copernicus Sentinel data 2026 · Source: ESA, Microsoft"
            " · Licence: proprietary (https://x/licence)"
            " · Provided via Microsoft Planetary Computer",
        )
        self.assertEqual(get.call_count, 1)  # cached per collection

    def test_unreachable_catalogue_still_names_the_provider(self):
        dem = COLLECTIONS_BY_KEY["cop-dem-30"]
        with mock.patch.object(stac, "get_json", side_effect=stac.StacError("down")):
            text = recipes.attribution(dem, "2021-04-22")
        self.assertEqual(text, "Provided via Microsoft Planetary Computer")

    def test_null_lists_and_licence_without_link(self):
        landsat = COLLECTIONS_BY_KEY["landsat-c2-l2"]
        info = {"providers": None, "license": "CC0-1.0", "links": None}
        with mock.patch.object(stac, "get_json", return_value=info):
            text = recipes.attribution(landsat, "2026-01-01")
        self.assertEqual(
            text, "Licence: CC0-1.0 · Provided via Microsoft Planetary Computer"
        )

    def test_static_catalogue_uses_the_licence_it_ships(self):
        static = next(
            c
            for c in COLLECTIONS_BY_KEY.values()
            if PROVIDERS[c.provider].search_method == "STATIC" and c.license
        )
        with mock.patch.object(stac, "get_json") as get:
            text = recipes.attribution(static, "2020-01-01")
        get.assert_not_called()
        self.assertIn(f"Licence: {static.license}", text)


# -- catalogue search --------------------------------------------------------------

_AOI = {
    "type": "Polygon",
    "coordinates": [[[6.9, 50.9], [7.0, 50.9], [7.0, 51.0], [6.9, 51.0], [6.9, 50.9]]],
}


def _feature(
    item_id: str, day: str, cloud: float | None = None, where=_FOOTPRINT
) -> dict:
    props: dict = {"datetime": f"{day}T10:00:00Z"}
    if cloud is not None:
        props["eo:cloud_cover"] = cloud
    return {"id": item_id, "geometry": where, "properties": props, "assets": {}}


class StacSearchTest(unittest.TestCase):
    """``stac.search`` against a fake API: request shape and result filtering."""

    def run_search(
        self, provider_key, collection, pages, max_cloud=None, max_items=50, aoi=_AOI
    ):
        calls: list[tuple[str, dict | None]] = []
        replies = iter(pages)

        def get_json(url, body=None, feedback=None):
            calls.append((url, body))
            return next(replies)

        with mock.patch.object(stac, "get_json", side_effect=get_json):
            items, discarded = stac.search(
                PROVIDERS[provider_key],
                collection,
                aoi,
                "2026-09-01",
                "2026-09-15",
                max_cloud,
                max_items,
            )
        return items, discarded, calls

    def test_post_request_with_server_side_cloud_filter(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        _, _, calls = self.run_search(
            "pc", s2, [{"features": []}], max_cloud=30, max_items=250
        )
        ((url, sent),) = calls
        self.assertIsNotNone(sent)
        body = sent or {}
        self.assertTrue(url.endswith("/search"))
        self.assertEqual(body["collections"], ["sentinel-2-l2a"])
        self.assertEqual(body["limit"], 100)  # page size is capped
        self.assertEqual(body["datetime"], "2026-09-01T00:00:00Z/2026-09-15T23:59:59Z")
        self.assertEqual(body["query"], {"eo:cloud_cover": {"lte": 30}})
        self.assertEqual(
            body["sortby"], [{"field": "properties.datetime", "direction": "desc"}]
        )

    def test_no_cloud_filter_without_limit_and_no_period_for_static_products(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        for max_cloud in (None, 100):
            with self.subTest(max_cloud=max_cloud):
                _, _, calls = self.run_search("pc", s2, [{"features": []}], max_cloud)
                self.assertNotIn("query", calls[0][1] or {})
        dem = COLLECTIONS_BY_KEY["cop-dem-30"]
        _, _, calls = self.run_search("pc", dem, [{"features": []}], 30, max_items=10)
        body = calls[0][1] or {}
        self.assertNotIn("datetime", body)
        self.assertNotIn("query", body)  # the DEM has no cloud cover
        self.assertEqual(body["limit"], 10)

    def test_cloud_filter_on_the_client_where_the_api_has_none(self):
        s2 = dataclasses.replace(COLLECTIONS_BY_KEY["s2-l2a"], provider="dlr")
        page = {
            "features": [
                _feature("c10", "2026-09-10", 10),
                _feature("c30", "2026-09-09", 30),  # at the limit: kept
                _feature("c31", "2026-09-08", 31),
                _feature("unknown", "2026-09-07"),  # no cloud cover: kept
            ]
        }
        items, _, calls = self.run_search("dlr", s2, [page], max_cloud=30)
        self.assertNotIn("query", calls[0][1] or {})
        self.assertEqual([i["id"] for i in items], ["c10", "c30", "unknown"])

    def test_pages_are_followed_duplicates_dropped_and_limit_kept(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        nxt = {
            "rel": "next",
            "href": "https://api/next",
            "method": "POST",
            "body": {"token": "p2"},
            "merge": True,
        }
        pages = [
            {
                "features": [_feature("a", "2026-09-01"), _feature("b", "2026-09-05")],
                "links": [nxt],
            },
            {
                "features": [
                    _feature("b", "2026-09-05"),
                    _feature("c", "2026-09-09"),
                    _feature("d", "2026-09-12"),
                ]
            },
        ]
        items, _, calls = self.run_search("pc", s2, pages, max_items=3)
        self.assertEqual([i["id"] for i in items], ["c", "b", "a"])  # newest first
        self.assertEqual(calls[1][0], "https://api/next")
        self.assertEqual((calls[1][1] or {})["token"], "p2")
        self.assertEqual(
            (calls[1][1] or {})["collections"], ["sentinel-2-l2a"]
        )  # merged

    def test_scenes_beside_the_area_are_discarded(self):
        far = {
            "type": "Polygon",
            "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]],
        }
        page = {
            "features": [
                _feature("hit", "2026-09-10"),
                _feature("miss", "2026-09-09", where=far),
            ]
        }
        items, discarded, _ = self.run_search(
            "pc", COLLECTIONS_BY_KEY["s2-l2a"], [page]
        )
        self.assertEqual([i["id"] for i in items], ["hit"])
        self.assertEqual(discarded, 1)
        self.assertAlmostEqual(items[0][stac.COVERAGE_KEY], 100.0, delta=0.01)

    def test_get_api_takes_one_bbox_per_side_of_the_antimeridian(self):
        fiji = {
            "type": "Polygon",
            "coordinates": [
                [[179.5, -17], [180.5, -17], [180.5, -16], [179.5, -16], [179.5, -17]]
            ],
        }
        c = dataclasses.replace(COLLECTIONS_BY_KEY["s2-l2a"], provider="deafrica")
        _, _, calls = self.run_search(
            "deafrica", c, [{"features": []}, {"features": []}], aoi=fiji
        )
        self.assertEqual(len(calls), 2)
        bboxes = sorted(parse_qs(urlparse(url).query)["bbox"][0] for url, _ in calls)
        self.assertEqual(
            bboxes, ["-180.0,-17.0,-179.5,-16.0", "179.5,-17.0,180.0,-16.0"]
        )
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(
            query["datetime"], ["2026-09-01T00:00:00Z/2026-09-15T23:59:59Z"]
        )
        self.assertEqual(query["sortby"], ["-properties.datetime"])
        self.assertIsNone(calls[0][1])


class SignerTest(unittest.TestCase):
    blob = "https://acct.blob.core.windows.net/container/a/b.tif"

    def token_reply(self, minutes: float) -> dict:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        return {"token": f"sv=x&m={minutes}", "msft:expiry": expiry.isoformat()}

    def test_signer_per_provider(self):
        self.assertIsInstance(
            stac.signer_for(PROVIDERS["pc"]), stac.PlanetaryComputerSigner
        )
        self.assertIsInstance(
            stac.signer_for(PROVIDERS["earthsearch"]), stac.NullSigner
        )

    def test_tokens_are_cached_per_container_until_shortly_before_expiry(self):
        signer = stac.PlanetaryComputerSigner()
        with mock.patch.object(
            stac, "get_json", return_value=self.token_reply(45)
        ) as get:
            first = signer.sign(self.blob)
            second = signer.sign(self.blob.replace("a/b", "c/d"))
            self.assertEqual(get.call_count, 1)
            self.assertEqual(first, self.blob + "?sv=x&m=45")
            self.assertTrue(second.endswith("?sv=x&m=45"))
            signer.sign(self.blob.replace("/container/", "/other/"))
            self.assertEqual(get.call_count, 2)
            signer.invalidate()
            signer.sign(self.blob)
            self.assertEqual(get.call_count, 3)

    def test_token_is_appended_to_an_existing_query(self):
        signer = stac.PlanetaryComputerSigner()
        with mock.patch.object(stac, "get_json", return_value=self.token_reply(45)):
            signed = signer.sign(self.blob + "?version=2")
        self.assertEqual(signed, self.blob + "?version=2&sv=x&m=45")

    def test_token_close_to_expiry_is_renewed(self):
        signer = stac.PlanetaryComputerSigner()
        with mock.patch.object(
            stac, "get_json", return_value=self.token_reply(10)
        ) as get:
            signer.sign(self.blob)
            signer.sign(self.blob)
        self.assertEqual(get.call_count, 2)

    def test_other_hosts_are_not_signed_and_local_paths_refused(self):
        signer = stac.PlanetaryComputerSigner()
        with mock.patch.object(stac, "get_json") as get:
            self.assertEqual(
                signer.sign("https://example.org/x.tif"), "https://example.org/x.tif"
            )
            get.assert_not_called()
        for other in (stac.PlanetaryComputerSigner(), stac.NullSigner()):
            with self.subTest(type(other).__name__), self.assertRaises(stac.StacError):
                other.sign("/vsimem/x.tif")


if __name__ == "__main__":
    unittest.main()
