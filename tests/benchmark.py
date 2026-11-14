"""Baseline timing: search, clear-sky estimate and download.

Usage: python-qgis.bat tests/benchmark.py [output.json]
"""

import json
import os
import sys
import time

import qgis_app

qgis_app.start()

from osgeo import gdal, ogr, osr
from qgis.core import QgsFeedback

from satellite_downloader.core import clouds, geometry, stac
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY as C
from satellite_downloader.core.catalog import PROVIDERS as P
from satellite_downloader.core.pipeline import (
    GDAL_HTTP_OPTIONS,
    Downloader,
    DownloadJob,
    group_key,
)

gdal.UseExceptions()

PLACES = {
    "Cologne": (6.95, 50.94),
    "Atacama": (-69.9, -24.4),
    "Svalbard": (15.6, 78.22),
}
SIZES_KM = (10, 50)


def square(lon, lat, km):
    """Square AOI of ``km`` side length around a point, as WGS84 WKT."""
    utm = geometry.srs(
        geometry.auto_crs(ogr.CreateGeometryFromWkt(f"POINT ({lon} {lat})"))
    )
    to_utm = osr.CoordinateTransformation(geometry.srs("EPSG:4326"), utm)
    x, y, _ = to_utm.TransformPoint(lon, lat)
    h = km * 500
    ring = ogr.CreateGeometryFromWkt(
        f"POLYGON(({x - h} {y - h},{x + h} {y - h},{x + h} {y + h},{x - h} {y + h},{x - h} {y - h}))"
    )
    ring.Segmentize(1000)
    ring.Transform(osr.CoordinateTransformation(utm, geometry.srs("EPSG:4326")))
    return ring.ExportToWkt()


def newest_group(items, collection):
    if not items:
        return []
    key = group_key(items[0], collection, True)
    return [i for i in items if group_key(i, collection, True) == key]


def measure(product, wkt, place, km):
    c = C[product]
    aoi = json.loads(ogr.CreateGeometryFromWkt(wkt).ExportToJson())
    row = {"place": place, "km": km, "product": product}
    t = time.monotonic()
    items, _ = stac.search(
        P[c.provider],
        c,
        aoi,
        "2026-06-01",
        "2026-09-15",
        30 if c.has_cloud_cover else None,
        20,
    )
    row["search_s"] = round(time.monotonic() - t, 1)
    if c.mask is not None and items:
        t = time.monotonic()
        clouds.estimate(
            items[:5],
            c,
            geometry.from_geojson(aoi),
            stac.signer_for(P[c.provider]),
            GDAL_HTTP_OPTIONS,
        )
        row["clouds_5_scenes_s"] = round(time.monotonic() - t, 1)
    group = newest_group(items, c)
    if not group:
        row["result"] = "no scenes"
        return row
    assets = list(c.default_assets)
    if product == "s1-rtc":
        assets = [p for p in ("vv", "vh", "hh", "hv") if p in group[0]["assets"]][:2]
    out = os.path.join(qgis_app.OUTPUT, "benchmark", f"{place}_{km}_{product}")
    job = DownloadJob(
        P[c.provider], c, group, assets, out, wkt, False, c.native_res, True
    )
    t = time.monotonic()
    d = Downloader(job, QgsFeedback(), lambda m, _l: None)
    d.run()
    row["download_s"] = round(time.monotonic() - t, 1)
    row["scenes"] = len(group)
    row["bands"] = len(assets)
    if d.result.outputs:
        ds = gdal.Open(d.result.outputs[0])
        row["pixel"] = f"{ds.RasterXSize}x{ds.RasterYSize}"
        row["mb"] = round(os.path.getsize(d.result.outputs[0]) / 1e6, 1)
    row["result"] = "ok" if d.result.outputs else f"empty/error ({d.result.failures})"
    return row


def main():
    rows = []
    for place, (lon, lat) in PLACES.items():
        for km in SIZES_KM:
            wkt = square(lon, lat, km)
            for product in ("s2-l2a", "s1-rtc", "cop-dem-30"):
                try:
                    row = measure(product, wkt, place, km)
                except Exception as exc:  # noqa: BLE001 - record the failure, keep measuring
                    row = {
                        "place": place,
                        "km": km,
                        "product": product,
                        "result": str(exc)[:120],
                    }
                print(json.dumps(row, ensure_ascii=False), flush=True)
                rows.append(row)
    target = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(qgis_app.OUTPUT, "benchmark.json")
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
