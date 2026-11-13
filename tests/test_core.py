"""Offline tests for the core logic (no network)."""

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import qgis_app

qgis_app.start()

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import QgsFeedback

from satellite_downloader.core import (
    clouds,
    geometry,
    places,
    recipes,
    stac,
)
from satellite_downloader.core.catalog import (
    CBERS_CMASK,
    COLLECTIONS_BY_KEY,
    LANDSAT_QA_MASK,
    PROVIDERS,
    S2_SCL_MASK,
    Collection,
)
from satellite_downloader.core.pipeline import (
    FALLBACK_NODATA,
    Downloader,
    DownloadJob,
    common_nodata,
    group_key,
    safe_name,
    type_holding,
)


def polygon(*coords):
    return {
        "type": "Polygon",
        "coordinates": [list(map(list, coords)) + [list(coords[0])]],
    }


class AntimeridianTest(unittest.TestCase):
    fiji = polygon((179.5, -17.0), (-179.5, -17.0), (-179.5, -16.0), (179.5, -16.0))

    def test_unwrap_shifts_negative_longitudes(self):
        lons = [p[0] for p in geometry.unwrap_antimeridian(self.fiji)["coordinates"][0]]
        self.assertEqual(sorted(set(lons)), [179.5, 180.5])

    def test_ordinary_polygon_untouched(self):
        cologne = polygon((6.9, 50.9), (7.0, 50.9), (7.0, 51.0), (6.9, 51.0))
        self.assertEqual(geometry.unwrap_antimeridian(cologne), cologne)

    def test_split_folds_into_valid_range_and_keeps_area(self):
        unwrapped = geometry.from_geojson(geometry.unwrap_antimeridian(self.fiji))
        split = geometry.split_antimeridian(unwrapped)
        xmin, xmax, _, _ = split.GetEnvelope()
        self.assertGreaterEqual(xmin, -180)
        self.assertLessEqual(xmax, 180)
        self.assertEqual(split.GetGeometryCount(), 2)
        self.assertAlmostEqual(split.GetArea(), unwrapped.GetArea(), places=6)
        lons = sorted(
            {
                round(p[0], 9)
                for poly in geometry.to_geojson(split)["coordinates"]
                for ring in poly
                for p in ring
            }
        )
        self.assertEqual(lons, [-180.0, -179.5, 179.5, 180.0])

    def test_geometry_ending_at_180_is_not_split(self):
        edge = geometry.from_geojson(
            polygon((179.0, -17.0), (180.0, -17.0), (180.0, -16.0), (179.0, -16.0))
        )
        result = geometry.split_antimeridian(edge)
        self.assertEqual(result.GetGeometryName(), "POLYGON")

    def test_genuinely_global_extent_is_not_unwrapped(self):
        band = polygon(
            (-170, -10), (-10, -10), (10, -10), (170, -10), (170, 10), (-170, 10)
        )
        self.assertEqual(geometry.unwrap_antimeridian(band), band)

    def test_search_geometry_for_fiji(self):
        query, aoi = stac.search_geometry(self.fiji)
        self.assertEqual(query["type"], "MultiPolygon")
        east_scene = polygon(
            (-179.8, -17.5), (-178.0, -17.5), (-178.0, -15.5), (-179.8, -15.5)
        )
        self.assertGreater(geometry.aoi_coverage(east_scene, aoi), 0)


class SearchGeometryTest(unittest.TestCase):
    def test_simplified_geometry_still_covers_detailed_polygon(self):
        circle = ogr.CreateGeometryFromWkt("POINT (6.95 50.94)").Buffer(0.2, 1000)
        self.assertGreater(geometry.vertex_count(circle), 500)
        simple = geometry.simplify_covering(circle, 500)
        self.assertLessEqual(geometry.vertex_count(simple), 500)
        self.assertTrue(simple.Contains(circle))
        # Covering, but tight: a few per mille larger, not a coarse blob
        self.assertLess(simple.GetArea() / circle.GetArea(), 1.02)

    def test_small_geometry_is_returned_unchanged(self):
        square = geometry.from_geojson(polygon((0, 0), (1, 0), (1, 1), (0, 1)))
        self.assertIs(geometry.simplify_covering(square, 5), square)  # 5 vertices

    def test_coverage_with_invalid_footprint_and_point_aoi(self):
        aoi = geometry.from_geojson(polygon((0, 0), (2, 0), (2, 2), (0, 2)))
        bow_tie = polygon((0, 0), (2, 2), (2, 0), (0, 2))  # self-intersecting
        self.assertAlmostEqual(geometry.aoi_coverage(bow_tie, aoi) or 0.0, 50.0)
        point = ogr.CreateGeometryFromWkt("POINT (1 1)")
        self.assertEqual(
            geometry.aoi_coverage(polygon((0, 0), (2, 0), (2, 2)), point), 100.0
        )
        self.assertEqual(
            geometry.aoi_coverage(polygon((5, 5), (6, 5), (6, 6)), point), 0.0
        )

    def test_coverage(self):
        aoi = geometry.from_geojson(polygon((0, 0), (2, 0), (2, 2), (0, 2)))
        half = polygon((1, -1), (3, -1), (3, 3), (1, 3))
        self.assertAlmostEqual(geometry.aoi_coverage(half, aoi), 50.0)
        self.assertEqual(
            geometry.aoi_coverage(polygon((5, 5), (6, 5), (6, 6)), aoi), 0.0
        )
        self.assertIsNone(geometry.aoi_coverage(None, aoi))


class CrsTest(unittest.TestCase):
    def crs_for(self, lon, lat, size=0.1):
        return geometry.auto_crs(
            geometry.from_geojson(
                polygon(
                    (lon, lat),
                    (lon + size, lat),
                    (lon + size, lat + size),
                    (lon, lat + size),
                )
            )
        )

    def test_utm(self):
        self.assertEqual(self.crs_for(6.9, 50.9), "EPSG:32632")
        self.assertEqual(self.crs_for(-70.5, -24.5), "EPSG:32719")

    def test_polar(self):
        self.assertEqual(self.crs_for(10, 85), "EPSG:3995")
        self.assertEqual(self.crs_for(166, -85), "EPSG:3031")

    def test_wide_area_gets_equal_area(self):
        code = self.crs_for(0, 40, size=20)
        self.assertTrue(code.startswith("+proj=laea"))
        self.assertTrue(geometry.srs(code).IsProjected())

    def test_unwrapped_fiji_gets_valid_utm_zone(self):
        code = geometry.auto_crs(
            geometry.from_geojson(geometry.unwrap_antimeridian(AntimeridianTest.fiji))
        )
        self.assertIn(code, ("EPSG:32760", "EPSG:32701"))

    def test_limits_are_still_utm(self):
        # Centroid exactly at 84° N / 80° S, width exactly 12°: still UTM
        self.assertEqual(self.crs_for(10, 83.5, size=1.0), "EPSG:32632")
        self.assertEqual(self.crs_for(10, -80.5, size=1.0), "EPSG:32732")
        self.assertEqual(self.crs_for(0, 40, size=12.0), "EPSG:32632")  # centroid 6°E

    def test_utm_zone_edges_and_equator(self):
        self.assertEqual(self.crs_for(5.95, -0.05), "EPSG:32632")  # centroid 6°E, 0°N
        self.assertEqual(self.crs_for(-180.0, 10, size=0.1), "EPSG:32601")
        self.assertEqual(self.crs_for(179.8, 10, size=0.1), "EPSG:32660")

    def test_resolution_in_geographic_and_projected_crs(self):
        wgs84 = geometry.srs("EPSG:4326")
        self.assertAlmostEqual(geometry.resolution_in_crs(111_320, wgs84), 1.0)
        self.assertEqual(geometry.resolution_in_crs(30, geometry.srs("EPSG:32632")), 30)

    def test_snap_bounds_with_an_arbitrary_origin(self):
        # Origin 20 m / 10 m off the 30 m multiples; on-grid edges stay put
        snapped = geometry.snap_bounds(
            (354530, 5647400, 354600, 5647470), 30, (278090.0, 5691310.0)
        )
        self.assertEqual(snapped, (354530, 5647390, 354620, 5647480))

    def test_snap_bounds_keeps_landsat_half_pixel_origin(self):
        snapped = geometry.snap_bounds(
            (354530, 5647400, 354600, 5647470), 30, (278085.0, 5691315.0)
        )
        self.assertEqual([v % 30 for v in snapped], [15, 15, 15, 15])
        self.assertLessEqual(snapped[0], 354530)
        self.assertGreaterEqual(snapped[2], 354600)


class MaskTest(unittest.TestCase):
    def test_scl_classes(self):
        values = np.array([-1, 0, 1, 4, 5, 8, 9, 3, 10, 6])
        self.assertEqual(clouds.classify(values, S2_SCL_MASK), (3, 4))

    def test_landsat_bits(self):
        fill, clear, cloud, shadow = 1, 1 << 6, (1 << 3) | (1 << 1), 1 << 4
        values = np.array([fill, clear, clear, cloud, shadow, -1])
        self.assertEqual(clouds.classify(values, LANDSAT_QA_MASK), (2, 2))

    def test_landsat_bits_with_confidence_bits_set(self):
        # Real QA_PIXEL codes also carry the confidence bits 8-15, so a check
        # that reads the wrong bit or shifts the wrong way changes the result.
        codes = {
            21824: "clear land",
            21952: "clear water",
            22280: "cloud, high confidence",
            23888: "cloud shadow",
            54596: "cirrus",
            1: "fill",
        }
        values = np.array(list(codes))
        self.assertEqual(clouds.classify(values, LANDSAT_QA_MASK), (2, 3))

    def test_cmask_clear_list(self):
        self.assertEqual(
            clouds.classify(np.array([0, 127, 127, 255, 200]), CBERS_CMASK), (2, 2)
        )


class PipelineHelpersTest(unittest.TestCase):
    def item(self, item_id, when, **props):
        return {"id": item_id, "properties": {"datetime": when, **props}}

    def test_sentinel1_passes_are_kept_apart(self):
        rtc = COLLECTIONS_BY_KEY["s1-rtc"]
        asc = self.item(
            "a",
            "2026-09-13T17:15:00Z",
            **{"sat:orbit_state": "ascending", "sat:relative_orbit": 88},
        )
        desc = self.item(
            "d",
            "2026-09-13T05:41:00Z",
            **{"sat:orbit_state": "descending", "sat:relative_orbit": 139},
        )
        self.assertEqual(group_key(asc, rtc, True), "sentinel-1-rtc_20260913_asc_R088")
        self.assertNotEqual(group_key(asc, rtc, True), group_key(desc, rtc, True))
        self.assertEqual(group_key(asc, rtc, False), "a")

    def test_landsat_path_and_missing_properties(self):
        landsat = COLLECTIONS_BY_KEY["landsat-c2-l2"]
        self.assertEqual(
            group_key(
                self.item("x", "2026-08-11T10:00:00Z", **{"landsat:wrs_path": "196"}),
                landsat,
                True,
            ),
            "landsat-c2-l2_20260811_P196",
        )
        dem = COLLECTIONS_BY_KEY["cop-dem-30"]
        self.assertEqual(
            group_key(self.item("t", "2021-04-22T00:00:00Z"), dem, True),
            "cop-dem-glo-30_20210422",
        )

    def test_type_holding(self):
        self.assertEqual(type_holding(gdal.GDT_Byte, -9999), gdal.GDT_Int16)
        self.assertEqual(type_holding(gdal.GDT_UInt16, 0), gdal.GDT_UInt16)
        self.assertEqual(type_holding(gdal.GDT_UInt16, -9999), gdal.GDT_Int32)
        self.assertEqual(type_holding(gdal.GDT_Int16, -0.5), gdal.GDT_Float32)


class StacHelpersTest(unittest.TestCase):
    def test_next_page_post_merge(self):
        page = {
            "links": [
                {
                    "rel": "next",
                    "href": "u",
                    "method": "POST",
                    "body": {"page": 2},
                    "merge": True,
                }
            ]
        }
        self.assertEqual(
            stac.next_page(page, {"limit": 10}), ("u", {"limit": 10, "page": 2})
        )
        self.assertEqual(stac.next_page({"links": []}, {}), (None, None))

    def test_signer_only_touches_azure_blobs(self):
        signer = stac.PlanetaryComputerSigner()
        self.assertEqual(
            signer.sign("https://data.inpe.br/x.tif"), "https://data.inpe.br/x.tif"
        )
        from datetime import datetime, timedelta, timezone

        signer._tokens[("acc", "cont")] = (
            "st=1&sig=abc",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.assertEqual(
            signer.sign("https://acc.blob.core.windows.net/cont/a/b.tif"),
            "https://acc.blob.core.windows.net/cont/a/b.tif?st=1&sig=abc",
        )
        signer.invalidate()
        self.assertEqual(signer._tokens, {})


class UntrustedCatalogueDataTest(unittest.TestCase):
    """Catalogue responses must not steer reads or writes outside the web/output."""

    def test_only_web_addresses_are_fetched(self):
        for href in (
            "file:///C:/Windows/win.ini",
            "/vsizip/C:/secret.zip/a.tif",
            "C:\\Users\\x\\a.tif",
            "ftp://example.org/a.tif",
        ):
            with self.subTest(href):
                with self.assertRaises(stac.StacError):
                    stac.NullSigner().sign(href)
                with self.assertRaises(stac.StacError):
                    stac.PlanetaryComputerSigner().sign(href)
                with self.assertRaises(stac.StacError):
                    stac.request(href)
        self.assertEqual(stac.web_url("HTTPS://a.org/x"), "HTTPS://a.org/x")

    def test_names_from_catalogues_stay_one_path_component(self):
        for raw in ("../../evil", "..\\..\\evil", "C:\\Windows\\x", "/etc/x", "..", ""):
            with self.subTest(raw):
                name = safe_name(raw)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertNotIn(":", name)
                self.assertFalse(name.startswith("."))
                self.assertTrue(name)

    def test_ordinary_ids_are_unchanged(self):
        # Existing results are found again only if their names stay the same.
        for raw in (
            "S2A_MSIL2A_20260913T103021_R108_T32ULB_20260913T150000",
            "sentinel-1-rtc_20260913_asc_R088",
            "ch.swisstopo.swissimage-dop10",
            "CB4A-WFI-L4-SR-1",
        ):
            with self.subTest(raw):
                self.assertEqual(safe_name(raw), raw)


class PlacesTest(unittest.TestCase):
    def test_decimal_pairs(self):
        self.assertEqual(places.parse_coordinates("50.94, 6.95"), (6.95, 50.94))
        self.assertEqual(places.parse_coordinates("50,94; 6,95"), (6.95, 50.94))
        self.assertEqual(
            places.parse_coordinates("-120.5 35.2"), (-120.5, 35.2)
        )  # lon first
        self.assertIsNone(places.parse_coordinates("Atacama"))
        self.assertIsNone(places.parse_coordinates("95, 200"))

    def test_dms_and_german_east(self):
        lon, lat = places.parse_coordinates("50°56'24\"N 6°57'0\"O")
        self.assertAlmostEqual(lat, 50.94)
        self.assertAlmostEqual(lon, 6.95)
        lon, lat = places.parse_coordinates("24°24'S 69°54'W")
        self.assertAlmostEqual(lat, -24.4)
        self.assertAlmostEqual(lon, -69.9)

    def test_hemisphere_letters_before_or_after_the_number(self):
        cases = {
            "N50.94 E6.95": (6.95, 50.94),
            "E6.95 N50.94": (6.95, 50.94),
            "50.94N 6.95E": (6.95, 50.94),
            "6.95O, 50.94N": (6.95, 50.94),  # German "Ost"
            "S33.9 W18.4": (-18.4, -33.9),
            "33.9S 18.4W": (-18.4, -33.9),
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(places.parse_coordinates(text), expected)
        self.assertIsNone(places.parse_coordinates("N50 N6"))  # two latitudes

    def test_dms_without_minutes_and_two_latitudes(self):
        self.assertEqual(places.parse_coordinates("50°N 6°E"), (6.0, 50.0))
        self.assertEqual(places.parse_coordinates("50°30'N 6°E"), (6.0, 50.5))
        self.assertIsNone(places.parse_coordinates("50°N 6°S"))

    def test_decimal_pairs_at_the_limits(self):
        # The first value is a longitude only if it cannot be a latitude.
        self.assertEqual(places.parse_coordinates("90, 100"), (100.0, 90.0))
        self.assertEqual(places.parse_coordinates("100, 90"), (100.0, 90.0))
        self.assertEqual(places.parse_coordinates("-90, 180"), (180.0, -90.0))
        self.assertEqual(places.parse_coordinates("90, -180"), (-180.0, 90.0))
        self.assertIsNone(places.parse_coordinates("91, 181"))

    def test_utm_bands_around_the_equator(self):
        north = places.parse_coordinates("31N 500000 110000")
        south = places.parse_coordinates("31M 500000 9890000")
        self.assertIsNotNone(north)
        self.assertIsNotNone(south)
        self.assertAlmostEqual(north[1] if north else 0, 0.995, delta=0.01)
        self.assertAlmostEqual(south[1] if south else 0, -0.995, delta=0.01)
        cape_town = places.parse_coordinates("34H 261880 6243530")
        self.assertIsNotNone(cape_town)
        lon, lat = cape_town or (0.0, 0.0)
        self.assertAlmostEqual(lon, 18.42, delta=0.05)
        self.assertAlmostEqual(lat, -33.92, delta=0.05)

    def test_extent_km(self):
        width, height = places._extent_km((0.0, 1.0, 10.0, 11.0))  # S, N, W, E
        self.assertAlmostEqual(width, 111.32, delta=0.1)
        self.assertAlmostEqual(height, 111.32, delta=0.01)
        width, _ = places._extent_km((59.5, 60.5, 10.0, 11.0))
        self.assertAlmostEqual(width, 55.66, delta=0.1)  # cos(60°) = 0.5
        width, _ = places._extent_km((-17.0, -16.0, 179.0, -179.0))  # Fiji
        self.assertAlmostEqual(width, 2 * 111.32 * 0.9588, delta=0.5)

    def test_circle_size_is_reported(self):
        point = places.Place("x", 6.95, 50.94)
        area = places.area_for(point, radius_km=7.5)
        self.assertEqual((area.width_km, area.height_km), (15.0, 15.0))
        disc = ogr.CreateGeometryFromWkt(area.wkt)
        self.assertEqual(disc.GetGeometryRef(0).GetPointCount(), 64 + 1)

    def test_bbox_size_limits(self):
        # 0.45° of latitude ≈ 50 km: kept; 0.005° ≈ 0.56 km: a point
        edge = places.Place("edge", 7.0, 50.0, (49.80, 50.2491, 6.99, 7.01))
        self.assertEqual(places.area_for(edge).kind, "bbox")
        tiny = places.Place("tiny", 7.0, 50.0, (50.0, 50.005, 7.0, 7.005))
        self.assertEqual(places.area_for(tiny).kind, "circle")
        self.assertEqual(places.area_for(tiny).note, "")

    def test_utm(self):
        lon, lat = places.parse_coordinates("32U 356000 5645000")
        self.assertAlmostEqual(lon, 6.95, delta=0.01)
        self.assertAlmostEqual(lat, 50.94, delta=0.01)

    def test_point_becomes_circle(self):
        atacama = places.Place(
            "Desierto de Atacama",
            -69.42,
            -24.556,
            (-24.5564, -24.5563, -69.4227, -69.4226),
            "natural/desert",
        )
        area = places.area_for(atacama, radius_km=5)
        self.assertEqual(area.kind, "circle")
        disc = ogr.CreateGeometryFromWkt(area.wkt)
        ref = geometry.srs(geometry.auto_crs(disc))
        self.assertAlmostEqual(
            geometry.project(disc, ref).GetArea() / 1e6, 3.14159 * 25, delta=0.5
        )

    def test_medium_bbox_kept_and_huge_one_limited(self):
        cologne = places.Place(
            "Köln", 6.95, 50.94, (50.83, 51.08, 6.77, 7.16), "boundary"
        )
        self.assertEqual(places.area_for(cologne).kind, "bbox")
        svalbard = places.Place(
            "Svalbard", 17.0, 78.5, (74.0, 81.0, 10.0, 35.0), "boundary"
        )
        limited = places.area_for(svalbard, radius_km=10)
        self.assertEqual(limited.kind, "circle")
        self.assertIn("using a circle", limited.note)
        self.assertEqual(places.area_for(svalbard, whole_extent=True).kind, "bbox")


class RecipeHelpersTest(unittest.TestCase):
    def test_polar_night_is_computed(self):
        from datetime import date

        self.assertLess(recipes.noon_sun_elevation(78.2, date(2026, 12, 21)), 0)
        self.assertGreater(recipes.noon_sun_elevation(50.9, date(2026, 12, 21)), 10)
        back = recipes.next_daylight(78.2, date(2026, 12, 21))
        self.assertTrue(date(2027, 2, 1) < back < date(2027, 4, 1), back)
        message = recipes.explain_missing(
            recipes.RECIPES_BY_KEY["current"], 78.2, date(2026, 12, 21)
        )
        self.assertIn("Polar night", message)
        self.assertIn("Radar", message)

    def test_resolution_stays_on_native_multiples(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        self.assertEqual(recipes._resolution(s2, 10_000), 10.0)
        self.assertEqual(recipes._resolution(s2, 250_000), 30.0)  # 25 m needed -> 30 m

    def test_slug(self):
        self.assertEqual(
            recipes.slug("Köln, Nordrhein-Westfalen, Deutschland"), "koeln"
        )
        self.assertEqual(
            recipes.slug("Point 78.2200, 15.6000"), "point-78-2200-15-6000"
        )

    def test_s1_assets_fall_back_to_hh(self):
        rtc = COLLECTIONS_BY_KEY["s1-rtc"]
        polar = [{"assets": {"hh": {}, "hv": {}}}]
        self.assertEqual(
            recipes._assets(recipes.RECIPES_BY_KEY["radar"], rtc, polar), ["hh", "hv"]
        )
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        items = [{"assets": {"B04": {}, "B03": {}, "B02": {}}}]
        self.assertEqual(
            recipes._assets(recipes.RECIPES_BY_KEY["current"], s2, items),
            ["B04", "B03", "B02"],
        )

    def test_noon_sun_elevation(self):
        from datetime import date

        # 90° − |lat − declination|, declination ±23.44° at the solstices
        cases = [
            (0.0, date(2026, 3, 20), 90.0),
            (50.0, date(2026, 6, 21), 63.44),
            (50.0, date(2026, 12, 21), 16.56),
            (-33.9, date(2026, 12, 21), 79.54),
        ]
        for lat, day, expected in cases:
            with self.subTest(lat=lat, day=day):
                self.assertAlmostEqual(
                    recipes.noon_sun_elevation(lat, day), expected, delta=1.0
                )

    def test_explanations_name_the_searched_span(self):
        from datetime import date

        summer = date(2026, 6, 21)
        optical = recipes.explain_missing(recipes.RECIPES_BY_KEY["current"], 50, summer)
        self.assertIn("365 days", optical)
        radar = recipes.explain_missing(recipes.RECIPES_BY_KEY["radar"], 50, summer)
        self.assertIn("90 days", radar)
        dem = recipes.explain_missing(recipes.RECIPES_BY_KEY["dem"], 50, summer)
        self.assertIn("elevation model", dem)

    def test_dem_recipe_reprojects_bilinear(self):
        self.assertEqual(recipes.RECIPES_BY_KEY["dem"].resampling, "bilinear")
        self.assertEqual(recipes.RECIPES_BY_KEY["current"].resampling, "nearest")


# -- resampling and nodata through the real download pipeline ------------------

_UTM32 = "EPSG:32632"
_ORIGIN = (500_000.0, 5_600_000.0)
_SIZE = 512


def _write_raster(
    path: str,
    values: np.ndarray,
    res: float,
    nodata: float | None = None,
    overviews: str | None = None,
    data_type: int = gdal.GDT_Byte,
    origin: tuple[float, float] = _ORIGIN,
    crs: str = _UTM32,
) -> None:
    """GeoTIFF on a UTM 32N grid; ``values`` is (rows, cols) or (bands, rows, cols)."""
    stack = values if values.ndim == 3 else values[np.newaxis]
    ds = gdal.GetDriverByName("GTiff").Create(
        path, stack.shape[2], stack.shape[1], stack.shape[0], data_type, ["TILED=YES"]
    )
    ds.SetGeoTransform((origin[0], res, 0, origin[1], 0, -res))
    ds.SetProjection(geometry.srs(crs).ExportToWkt())
    for i, layer in enumerate(stack, start=1):
        band = ds.GetRasterBand(i)
        band.WriteArray(layer)
        if nodata is not None:
            band.SetNoDataValue(nodata)
    if overviews:
        ds.BuildOverviews(overviews, [2, 4, 8])
    ds = None


def _class_values() -> np.ndarray:
    """Diagonal stripes of SCL classes 4 (vegetation) and 8 (cloud)."""
    stripes = (np.indices((_SIZE, _SIZE)).sum(0) // 7) % 2 == 0
    return np.where(stripes, 4, 8).astype(np.uint8)


def _aoi_wkt(res: float, inset: float) -> str:
    """Diamond in EPSG:4326 well inside the test raster; corners fall outside."""
    span = _SIZE * res
    cx, cy, r = _ORIGIN[0] + span / 2, _ORIGIN[1] - span / 2, span / 2 - inset
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)):
        ring.AddPoint_2D(x, y)
    diamond = ogr.Geometry(ogr.wkbPolygon)
    diamond.AddGeometry(ring)
    diamond.Transform(
        osr.CoordinateTransformation(geometry.srs(_UTM32), geometry.srs("EPSG:4326"))
    )
    return diamond.ExportToWkt()


class _LocalDownloader(Downloader):
    """Reads asset hrefs as local paths; the real one only fetches web addresses."""

    def _source(self, item: dict, asset: str) -> str:
        return item["assets"][asset]["href"]


class PipelineResamplingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.logs: list[str] = []

    def collection(self, assets: tuple[str, ...], **kw) -> Collection:
        base = COLLECTIONS_BY_KEY["s2-l2a"]
        return dataclasses.replace(
            base, key="test", assets=assets, default_assets=assets, **kw
        )

    def run_job(
        self,
        collection: Collection,
        hrefs: dict[str, str],
        resolution_m: float,
        resampling: str,
        clip: bool = False,
    ) -> str:
        item = {
            "id": "scene",
            "properties": {"datetime": "2026-09-01T10:00:00Z", "proj:code": _UTM32},
            "assets": {a: {"href": h} for a, h in hrefs.items()},
        }
        job = DownloadJob(
            provider=PROVIDERS["earthsearch"],
            collection=collection,
            items=[item],
            assets=list(hrefs),
            out_dir=os.path.join(self.dir, "out"),
            aoi_wgs84_wkt=_aoi_wkt(collection.native_res, 200.0),
            clip_to_aoi=clip,
            resolution_m=resolution_m,
            mosaic_by_date=False,
            resampling=resampling,
        )
        result = _LocalDownloader(
            job, QgsFeedback(), lambda msg, _level: self.logs.append(msg)
        ).run()
        self.assertEqual(result.failures, 0, self.logs)
        self.assertEqual(len(result.outputs), 1, self.logs)
        return result.outputs[0]

    def test_class_band_never_gets_invented_classes(self):
        # Averaged overviews hold class 7 (and others) where 4 and 8 meet; a
        # coarser target made GDAL read them even with nearest neighbour.
        src = os.path.join(self.dir, "scl.tif")
        _write_raster(src, _class_values(), 20.0, overviews="AVERAGE")
        c = self.collection(("SCL",), native_res=20.0)
        for resampling in ("nearest", "bilinear", "average"):
            with self.subTest(resampling):
                out = self.run_job(c, {"SCL": src}, 60.0, resampling)
                ds = gdal.Open(out)
                values = set(np.unique(ds.ReadAsArray()).tolist())
                ds = None
                self.assertLessEqual(values, {0, 4, 8})
                self.assertTrue({4, 8} <= values)
                shutil.rmtree(os.path.join(self.dir, "out"))

    def test_measured_band_keeps_chosen_method_and_overviews(self):
        c = self.collection(("B04",), native_res=10.0)
        job = DownloadJob(
            PROVIDERS["earthsearch"],
            c,
            [{"id": "x", "properties": {}, "assets": {"B04": {}}}],
            ["B04"],
            self.dir,
            "POINT (0 0)",
            False,
            30.0,
            False,
            resampling="bilinear",
        )
        d = Downloader(job, QgsFeedback(), lambda _m, _l: None)
        self.assertEqual(d._resampling_for("B04"), ("bilinear", None))
        job.assets = ["SCL"]
        self.assertEqual(d._resampling_for("SCL"), ("mode", "NONE"))
        job.resolution_m = 10.0
        self.assertEqual(d._resampling_for("SCL"), ("near", "NONE"))

    def test_bands_with_different_nodata_share_one_in_the_file(self):
        # Before: band 2's nodata lived only in a .part.tif.aux.xml sidecar
        # that was left behind, so the GeoTIFF marked band 2 as all valid.
        values = np.full((_SIZE, _SIZE), 100, np.uint8)
        hrefs = {}
        for asset, nodata in (("a", 0), ("b", 255)):
            hrefs[asset] = os.path.join(self.dir, f"{asset}.tif")
            _write_raster(hrefs[asset], values, 10.0, nodata=nodata)
        c = self.collection(("a", "b"), native_res=10.0, nodata=None, mask=None)
        out = self.run_job(c, hrefs, 10.0, "nearest", clip=True)
        ds = gdal.Open(out)
        for b in (1, 2):
            band = ds.GetRasterBand(b)
            self.assertEqual(band.GetNoDataValue(), FALLBACK_NODATA)
            self.assertEqual(band.ReadAsArray()[0, 0], FALLBACK_NODATA)  # outside
            self.assertEqual(band.ReadAsArray()[_SIZE // 2, _SIZE // 2], 100)
        ds = None
        leftovers = [f for f in os.listdir(os.path.dirname(out)) if f.endswith(".xml")]
        self.assertEqual(leftovers, [])


class PipelineBehaviourTest(unittest.TestCase):
    """Output grid, CRS choice, file contents and retries of ``Downloader``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.out = os.path.join(self.dir, "out")
        self.logs: list[str] = []
        self.values = np.full((_SIZE, _SIZE), 100, np.uint8)

    def collection(self, assets: tuple[str, ...], **kw) -> Collection:
        kw.setdefault("mask", None)
        return dataclasses.replace(
            COLLECTIONS_BY_KEY["s2-l2a"],
            key="test",
            assets=assets,
            default_assets=assets,
            native_res=10.0,
            **kw,
        )

    def raster(self, name: str, values=None, **kw) -> str:
        path = os.path.join(self.dir, name)
        _write_raster(path, self.values if values is None else values, 10.0, **kw)
        return path

    def item(self, item_id: str, hrefs: dict[str, str], **props) -> dict:
        return {
            "id": item_id,
            "properties": {"datetime": "2026-09-01T10:00:00Z", **props},
            "assets": {a: {"href": h} for a, h in hrefs.items()},
        }

    def downloader(self, collection, items, assets, **kw) -> Downloader:
        job = DownloadJob(
            provider=PROVIDERS["earthsearch"],
            collection=collection,
            items=items,
            assets=assets,
            out_dir=self.out,
            aoi_wgs84_wkt=kw.pop("aoi", _aoi_wkt(10.0, 300.0)),
            clip_to_aoi=kw.pop("clip", False),
            resolution_m=kw.pop("resolution_m", 10.0),
            mosaic_by_date=False,
            **kw,
        )
        return _LocalDownloader(job, QgsFeedback(), lambda m, _l: self.logs.append(m))

    def test_output_grid_follows_the_source_origin(self):
        # Source pixels start 3 m and 7 m off the multiples of 10 m. Snapping to
        # plain multiples would shift every pixel; reading must copy them.
        values = np.arange(_SIZE * _SIZE, dtype=np.uint16).reshape(_SIZE, _SIZE)
        src = self.raster(
            "a.tif", values, data_type=gdal.GDT_UInt16, origin=(500_003.0, 5_600_007.0)
        )
        c = self.collection(("a",), nodata=None)
        result = self.downloader(
            c, [self.item("s", {"a": src}, **{"proj:code": _UTM32})], ["a"]
        ).run()
        ds = gdal.Open(result.outputs[0])
        x0, res, _, y0, _, _ = ds.GetGeoTransform()
        self.assertEqual(((x0 - 3) % 10, (y0 - 7) % 10, res), (0.0, 0.0, 10.0))
        # Pixel values are copied, not interpolated: they all exist in the source
        col, row = int((x0 - 500_003) / 10), int((5_600_007 - y0) / 10)
        out = ds.ReadAsArray()
        np.testing.assert_array_equal(out[0, :5], values[row, col : col + 5])
        ds = None

    def test_crs_shared_by_most_scenes_wins(self):
        # AOI lies in zone 32; two scenes in 33N and one in 32N
        hrefs = {"a": self.raster("a.tif")}
        items = [
            self.item("x", hrefs, **{"proj:epsg": 32633}),
            self.item("y", hrefs, **{"proj:epsg": 32633}),
            self.item("z", hrefs, **{"proj:code": _UTM32}),
        ]
        d = self.downloader(self.collection(("a",)), items, ["a"])
        d._choose_crs()
        self.assertEqual(d.dst_code, "EPSG:32633")
        self.assertIn("1 scene in another CRS will be resampled.", self.logs)

    def test_tie_prefers_the_utm_zone_of_the_area(self):
        hrefs = {"a": self.raster("a.tif")}
        items = [
            self.item("x", hrefs, **{"proj:code": "EPSG:32633"}),
            self.item("y", hrefs, **{"proj:code": _UTM32}),
        ]
        d = self.downloader(self.collection(("a",)), items, ["a"])
        d._choose_crs()
        self.assertEqual(d.dst_code, _UTM32)

    def test_crs_read_from_headers_when_the_catalogue_has_none(self):
        hrefs = {
            "a": self.raster("a.tif", crs="EPSG:32633", origin=(300_000.0, 5_600_000.0))
        }
        d = self.downloader(self.collection(("a",)), [self.item("x", hrefs)], ["a"])
        codes = d._choose_crs()
        self.assertEqual(codes, {"x": "EPSG:32633"})
        self.assertEqual(d.dst_code, "EPSG:32633")

    def test_explicit_target_crs_resamples_all_scenes(self):
        hrefs = {"a": self.raster("a.tif")}
        items = [self.item("x", hrefs, **{"proj:code": _UTM32})]
        d = self.downloader(
            self.collection(("a",)),
            items,
            ["a"],
            dst_crs_wkt=geometry.srs("EPSG:3035").ExportToWkt(),
        )
        result = d.run()  # reported once the headers are read
        self.assertEqual(d.dst_code, "EPSG:3035")
        self.assertEqual(len(result.outputs), 1, self.logs)
        self.assertIn(
            "No scene is in the target CRS; all pixels are resampled.", self.logs
        )
        ds = gdal.Open(result.outputs[0])
        self.assertEqual(geometry.auth_code(ds.GetSpatialRef()), "EPSG:3035")
        ds = None

    def test_data_types_nodata_band_names_and_sidecar(self):
        rgb = self.raster("rgb.tif", np.full((3, _SIZE, _SIZE), 7, np.uint8))
        dem = self.raster(
            "h.tif",
            np.full((_SIZE, _SIZE), 1200, np.uint16),
            data_type=gdal.GDT_UInt16,
            nodata=65535,
        )
        c = self.collection(("rgb", "h"), nodata=0)  # the collection's nodata wins
        d = self.downloader(
            c,
            [self.item("s", {"rgb": rgb, "h": dem}, **{"proj:code": _UTM32})],
            ["rgb", "h"],
        )
        result = d.run()
        ds = gdal.Open(result.outputs[0])
        names = [
            ds.GetRasterBand(i).GetDescription() for i in range(1, ds.RasterCount + 1)
        ]
        self.assertEqual(names, ["rgb_1", "rgb_2", "rgb_3", "h"])
        self.assertEqual(ds.GetRasterBand(1).DataType, gdal.GDT_UInt16)  # union
        self.assertEqual(ds.GetRasterBand(4).GetNoDataValue(), 0)
        self.assertEqual(ds.GetMetadataItem("STAC_ITEMS"), "s")
        ds = None
        sidecar = os.path.splitext(result.outputs[0])[0] + ".json"
        with open(sidecar, encoding="utf-8") as fh:
            params = json.load(fh)
        self.assertEqual(params["crs"], _UTM32)
        self.assertEqual(params["resolution"], 10.0)
        self.assertEqual(params["nodata"], 0)
        self.assertEqual(params["data_type"], "UInt16")
        self.assertEqual(params["resampling"], {"rgb": "nearest", "h": "nearest"})

    def test_settings_go_into_the_file_name(self):
        hrefs = {"a": self.raster("a.tif"), "SCL": self.raster("scl.tif")}
        items = [self.item("s", hrefs, **{"proj:code": _UTM32})]
        names = {}
        for alg in ("nearest", "bilinear"):
            for assets in (["a"], ["SCL"]):
                c = self.collection(tuple(assets), mask=S2_SCL_MASK)
                out = self.downloader(c, items, assets, resampling=alg).run().outputs[0]
                names[(alg, assets[0])] = os.path.basename(out)
        self.assertNotEqual(names[("nearest", "a")], names[("bilinear", "a")])
        # A class band is always resampled the same way, so the file is reused
        self.assertEqual(names[("nearest", "SCL")], names[("bilinear", "SCL")])

    def test_area_without_valid_pixels_is_skipped(self):
        src = self.raster("empty.tif", np.zeros((_SIZE, _SIZE), np.uint8), nodata=0)
        c = self.collection(("a",), nodata=0)
        result = self.downloader(
            c, [self.item("s", {"a": src}, **{"proj:code": _UTM32})], ["a"]
        ).run()
        self.assertEqual((result.outputs, result.empty, result.failures), ([], 1, 0))

    def test_expired_token_is_renewed_once(self):
        src = self.raster("a.tif")
        d = self.downloader(
            self.collection(("a",)),
            [self.item("s", {"a": src}, **{"proj:code": _UTM32})],
            ["a"],
        )
        d.signer = mock.Mock()
        real_warp = gdal.Warp
        calls = []

        def warp(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("HTTP error code: 403")
            return real_warp(*args, **kwargs)

        with mock.patch(
            "satellite_downloader.core.pipeline.gdal.Warp", side_effect=warp
        ):
            result = d.run()
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(len(calls), 2)
        d.signer.invalidate.assert_called_once()
        self.assertIn("Access token expired; got a new one.", self.logs)

    def test_other_errors_count_as_failures(self):
        src = self.raster("a.tif")
        d = self.downloader(
            self.collection(("a",)),
            [self.item("s", {"a": src}, **{"proj:code": _UTM32})],
            ["a"],
        )
        with mock.patch(
            "satellite_downloader.core.pipeline.gdal.Warp",
            side_effect=RuntimeError("HTTP error code: 500"),
        ):
            result = d.run()
        self.assertEqual((result.outputs, result.failures), ([], 1))
        self.assertEqual(os.listdir(self.out), [])  # no temporary files left

    def test_cancelled_before_start_writes_nothing(self):
        src = self.raster("a.tif")
        d = self.downloader(
            self.collection(("a",)),
            [self.item("s", {"a": src}, **{"proj:code": _UTM32})],
            ["a"],
        )
        d.feedback.cancel()
        self.assertEqual(d.run().outputs, [])

    def test_long_names_are_cut(self):
        self.assertEqual(len(safe_name("x" * 400)), 150)


class CategoricalRulesTest(unittest.TestCase):
    def test_is_categorical(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        self.assertTrue(s2.is_categorical("SCL"))  # mask asset
        for key in ("qa_pixel", "QA", "classification", "lccs_class", "burn-severity"):
            with self.subTest(key):
                self.assertTrue(s2.is_categorical(key))
        for key in ("B04", "red", "data", "visual", "prob"):
            with self.subTest(key):
                self.assertFalse(s2.is_categorical(key))
        self.assertTrue(s2.is_categorical("data", {"classification:classes": []}))
        self.assertTrue(
            s2.is_categorical(
                "data", {"raster:bands": [{"classification:classes": []}]}
            )
        )
        listed = dataclasses.replace(s2, categorical_assets=("data",))
        self.assertTrue(listed.is_categorical("data"))
        # Metadata without classification fields says nothing
        for meta in ({"title": "Data"}, {"raster:bands": [{"nodata": 0}]}):
            with self.subTest(meta=meta):
                self.assertFalse(s2.is_categorical("data", meta))

    def test_nodata_per_asset(self):
        landsat = COLLECTIONS_BY_KEY["landsat-c2-l2"]
        self.assertEqual(landsat.nodata_for("qa_pixel"), 1)  # QA fill value
        self.assertEqual(landsat.nodata_for("red"), 0)
        cbers = next(
            c for c in COLLECTIONS_BY_KEY.values() if "CMASK" in c.asset_nodata
        )
        self.assertEqual(cbers.nodata_for("CMASK"), 0)
        self.assertEqual(cbers.nodata_for("BAND13"), -9999)

    def test_common_nodata(self):
        byte, int16 = gdal.GDT_Byte, gdal.GDT_Int16
        self.assertEqual(
            common_nodata({"a": 0, "b": 255}, {"a": byte, "b": byte}), FALLBACK_NODATA
        )
        # -32768 cannot occur in a Byte band, so it can stand for both
        self.assertEqual(
            common_nodata({"a": -32768, "b": 0}, {"a": int16, "b": byte}), -32768
        )

    def test_east_west_metres(self):
        self.assertAlmostEqual(geometry.east_west_metres(30, 60), 15.0)
        self.assertAlmostEqual(geometry.east_west_metres(30, 0), 30.0)


if __name__ == "__main__":
    unittest.main()
