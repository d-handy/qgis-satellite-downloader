"""Turn ticked scenes and output settings into download jobs, without Qt.

The expert dialog collects the settings; this module decides what runs: one
job per dataset, the bands and resolution each job uses, and how large the
result will roughly be.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from ..core import selection
from ..core.catalog import COLLECTIONS_BY_KEY, MODE_RAW, PROVIDERS, Collection
from ..core.pipeline import DownloadJob, group_key, safe_name

LARGE_DOWNLOAD_BYTES = 5 * 1024**3
LARGE_SCENE_COUNT = 20
_BYTES_PER_VALUE = 4  # Float32 / Int32 upper bound for the estimate


@dataclass(frozen=True)
class OutputSettings:
    out_dir: str
    aoi_wgs84_wkt: str
    area_size_m: tuple[float, float]  # width, height of the AOI in UTM
    clip_to_aoi: bool
    mosaic_by_date: bool
    dst_crs_wkt: str | None
    resampling: str


@dataclass(frozen=True)
class BandChoice:
    """Bands and resolution picked in the dialog for the dataset it shows."""

    assets: list[str]
    resolution_m: float


@dataclass
class DownloadPlan:
    jobs: list[DownloadJob] = field(default_factory=list)
    estimate: int = 0  # bytes, uncompressed, windowed (COG) downloads only
    raw_scenes: int = 0  # whole-file downloads of unknown size

    def warning(self, scenes: int) -> str | None:
        return download_warning(scenes, self.raw_scenes, self.estimate)


def plan_downloads(
    items: list[dict], settings: OutputSettings, choice: BandChoice | None
) -> DownloadPlan:
    """One job per dataset among ``items``.

    ``choice`` applies when only the dataset shown in the dialog is involved;
    otherwise each dataset uses its default bands at native resolution.
    Raises ValueError when no band is chosen.
    """
    plan = DownloadPlan()
    for key, group in _by_collection(items).items():
        c = COLLECTIONS_BY_KEY[key]
        assets = choice.assets if choice else list(c.default_assets)
        if not assets:
            raise ValueError('No bands ticked. Tick at least one under "Bands".')
        res = choice.resolution_m if choice else c.native_res
        if c.mode == MODE_RAW:
            plan.raw_scenes += len(group)
        else:
            plan.estimate += _estimate(c, group, settings, len(assets), res)
        plan.jobs.append(_job(c, group, assets, res, settings))
    return plan


def _by_collection(items: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item[selection.COLLECTION_KEY], []).append(item)
    return groups


def _estimate(
    c: Collection,
    group: list[dict],
    settings: OutputSettings,
    n_assets: int,
    res: float,
) -> int:
    """Uncompressed bytes: pixels in the AOI × bands × output files.

    The files are counted with the pipeline's own ``group_key``, so a mosaic
    per day and orbit (Sentinel-1 ascending/descending) counts as two.
    """
    outputs = len({group_key(it, c, settings.mosaic_by_date) for it in group})
    width, height = settings.area_size_m
    pixels = math.ceil(width / res) * math.ceil(height / res)
    return pixels * n_assets * outputs * _BYTES_PER_VALUE


def _job(
    c: Collection,
    group: list[dict],
    assets: list[str],
    res: float,
    settings: OutputSettings,
) -> DownloadJob:
    return DownloadJob(
        provider=PROVIDERS[c.provider],
        collection=c,
        items=group,
        assets=assets,
        out_dir=os.path.join(settings.out_dir, safe_name(c.collection_id)),
        aoi_wgs84_wkt=settings.aoi_wgs84_wkt,
        clip_to_aoi=settings.clip_to_aoi,
        resolution_m=res,
        mosaic_by_date=settings.mosaic_by_date,
        dst_crs_wkt=settings.dst_crs_wkt,
        resampling=settings.resampling,
    )


def download_warning(scenes: int, raw_scenes: int, estimate: int) -> str | None:
    """Why a download deserves a second look, None if it is small and predictable."""
    parts = []
    if estimate > LARGE_DOWNLOAD_BYTES:
        parts.append(f"Estimated size: {estimate / 1024**3:.1f} GB uncompressed.")
    if scenes > LARGE_SCENE_COUNT:
        parts.append(f"{scenes} scenes take a while to download and process.")
    if raw_scenes == 1:
        parts.append(
            "1 scene is downloaded as a whole file, so its size is not known "
            "in advance."
        )
    elif raw_scenes:
        parts.append(
            f"{raw_scenes} scenes are downloaded as whole files, so their size is "
            "not known in advance."
        )
    return " ".join(parts) if parts else None
