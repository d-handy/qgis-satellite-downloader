"""Clear-sky share inside the area of interest, per scene.

The scene-wide ``eo:cloud_cover`` says little about a 5 km area: a scene can be
60 % cloudy and perfectly clear over the AOI. The mask band is therefore read
for the AOI only.

Mask bands are read at full resolution (``overviewLevel="NONE"``): the COG
overviews of Sentinel-2 SCL are averaged and contain class values that do not
exist (e.g. 12), so they cannot be classified.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from osgeo import gdal, ogr

from . import geometry
from .catalog import Collection, MaskSpec
from .stac import CLEAR_KEY

_OUTSIDE = -1  # warp fill for pixels outside the scene or the AOI
_MAX_PIXELS = 256  # long side of the sampling grid
_WORKERS = 6


def classify(values: np.ndarray, mask: MaskSpec) -> tuple[int, int]:
    """Count (clear, cloudy) pixels; fill and invalid pixels are ignored."""
    values = values[values != _OUTSIDE].astype(np.int64)
    if mask.kind == "bits":
        invalid = np.zeros(values.shape, bool)
        for bit in mask.invalid:
            invalid |= (values >> bit) & 1 == 1
        values = values[~invalid]
        cloudy = np.zeros(values.shape, bool)
        for bit in mask.cloudy:
            cloudy |= (values >> bit) & 1 == 1
    else:
        values = values[~np.isin(values, mask.invalid)]
        if mask.clear:
            cloudy = ~np.isin(values, mask.clear)
        else:
            cloudy = np.isin(values, mask.cloudy)
    n_cloudy = int(cloudy.sum())
    return int(values.size) - n_cloudy, n_cloudy


def _fill_value(mask: MaskSpec) -> int | None:
    """Source value of pixels without data, so tile collars do not overwrite."""
    if mask.fill is not None:
        return mask.fill
    if not mask.invalid:
        return None
    if mask.kind == "bits":
        return 1 << mask.invalid[0]
    return mask.invalid[0]


def clear_percent(
    item: dict | list[dict],
    collection: Collection,
    aoi_wgs84: ogr.Geometry,
    signer,
    feedback=None,
) -> float | None:
    """Clear share (%) of the observed AOI pixels, None if not determinable.

    ``item`` may be a list: tiles of one acquisition are then evaluated as one
    mosaic. ``aoi_wgs84`` must be unwrapped (see
    ``geometry.unwrap_antimeridian``), not split, so the projection is chosen
    for the AOI as a whole.
    """
    mask = collection.mask
    if mask is None:
        return None
    items = [
        it
        for it in (item if isinstance(item, list) else [item])
        if mask.asset in it.get("assets", {})
    ]
    if not items:
        return None
    sources = [
        "/vsicurl/" + signer.sign(it["assets"][mask.asset]["href"], feedback)
        for it in items
    ]
    values = _sample_mask(sources, mask, aoi_wgs84)
    if values is None:
        return None
    clear, cloudy = classify(values, mask)
    if clear + cloudy == 0:
        return None
    return 100.0 * clear / (clear + cloudy)


def _sample_mask(
    sources: list[str], mask: MaskSpec, aoi_wgs84: ogr.Geometry
) -> np.ndarray | None:
    """Mask values on a grid of at most ``_MAX_PIXELS`` across the AOI.

    Pixels outside the AOI or without data are ``_OUTSIDE``. Nearest neighbour
    at full resolution keeps class values intact.
    """
    ref = geometry.srs(geometry.auto_crs(aoi_wgs84))
    projected = geometry.project(aoi_wgs84, ref)
    xmin, xmax, ymin, ymax = projected.GetEnvelope()
    size = max(xmax - xmin, ymax - ymin)
    if size <= 0:
        return None
    fill = _fill_value(mask)
    with geometry.cutline_options(projected.ExportToWkt(), ref.ExportToWkt()) as cut:
        ds = gdal.Warp(
            "",
            sources,
            format="MEM",
            dstSRS=ref.ExportToWkt(),
            outputBounds=(xmin, ymin, xmax, ymax),
            xRes=size / _MAX_PIXELS,
            yRes=size / _MAX_PIXELS,
            outputType=gdal.GDT_Int32,
            srcNodata=fill if fill is not None else "None",
            dstNodata=_OUTSIDE,
            resampleAlg="near",
            overviewLevel="NONE",
            warpOptions=["INIT_DEST=NO_DATA"],
            **cut,
        )
    return None if ds is None else ds.GetRasterBand(1).ReadAsArray()


def estimate(
    items: list[dict],
    collection: Collection,
    aoi_wgs84: ogr.Geometry,
    signer,
    http_options: dict,
    feedback=None,
    on_result: Callable[[dict, float | None], None] | None = None,
) -> None:
    """Compute ``CLEAR_KEY`` for all items in parallel, newest first.

    ``on_result`` is called from worker threads as soon as an item is done.
    """

    def work(item):
        if feedback is not None and feedback.isCanceled():
            return item, None
        with gdal.ExceptionMgr(useExceptions=True), gdal.config_options(http_options):
            try:
                return item, clear_percent(
                    item, collection, aoi_wgs84, signer, feedback
                )
            except RuntimeError:
                return item, None

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(work, it) for it in items]
        for future in as_completed(futures):
            item, value = future.result()
            item[CLEAR_KEY] = value
            if on_result:
                on_result(item, value)
