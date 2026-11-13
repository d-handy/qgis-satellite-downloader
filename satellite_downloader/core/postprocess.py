"""Derived products written next to a download: dB, hillshade, slope."""

from __future__ import annotations

import os

import numpy as np
from osgeo import gdal

DB_NODATA = -9999.0
_BLOCK = 1024


def to_db(path: str) -> str:
    """Linear backscatter -> 10·log10, all bands, as ``<name>_dB.tif``."""
    target = os.path.splitext(path)[0] + "_dB.tif"
    if os.path.exists(target):
        return target
    src = gdal.Open(path)
    part = target + ".part.tif"
    dst = gdal.GetDriverByName("GTiff").Create(
        part,
        src.RasterXSize,
        src.RasterYSize,
        src.RasterCount,
        gdal.GDT_Float32,
        options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER"],
    )
    dst.SetGeoTransform(src.GetGeoTransform())
    dst.SetProjection(src.GetProjection())
    for b in range(1, src.RasterCount + 1):
        sband, dband = src.GetRasterBand(b), dst.GetRasterBand(b)
        nodata = sband.GetNoDataValue()
        dband.SetNoDataValue(DB_NODATA)
        dband.SetDescription(f"{sband.GetDescription()} [dB]".strip())
        for yoff in range(0, src.RasterYSize, _BLOCK):
            rows = min(_BLOCK, src.RasterYSize - yoff)
            arr = sband.ReadAsArray(0, yoff, src.RasterXSize, rows).astype(np.float64)
            valid = arr > 0
            if nodata is not None:
                valid &= arr != nodata
            out = np.full(arr.shape, DB_NODATA, np.float32)
            out[valid] = 10 * np.log10(arr[valid])
            dband.WriteArray(out, 0, yoff)
    dst.SetMetadata(src.GetMetadata())
    dst = None
    src = None
    os.replace(part, target)
    return target


def _dem_processing(path: str, mode: str, suffix: str, **options) -> str:
    target = os.path.splitext(path)[0] + suffix
    if not os.path.exists(target):
        part = target + ".part.tif"
        gdal.DEMProcessing(
            part,
            path,
            mode,
            computeEdges=True,
            creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
            **options,
        ).Close()
        os.replace(part, target)
    return target


def hillshade(path: str) -> str:
    """Multidirectional hillshade; the DEM must be in a metric CRS."""
    return _dem_processing(path, "hillshade", "_hillshade.tif", multiDirectional=True)


def slope(path: str) -> str:
    """Slope in degrees; the DEM must be in a metric CRS."""
    return _dem_processing(path, "slope", "_slope.tif", slopeFormat="degree")


STEPS = {"db": to_db, "hillshade": hillshade, "slope": slope}
