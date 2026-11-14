"""Download execution, independent of dialogs and tasks.

Cloud-optimised GeoTIFFs are never downloaded whole. GDAL reads only the
blocks that cover the area of interest over HTTP range requests (/vsicurl/),
warps them onto the pixel grid of the scenes and writes one multi-band GeoTIFF
per acquisition (day and orbit) or per scene. Swath products (Sentinel-3) are
streamed as whole files and can resume after an interruption.

Progress and cancellation go through a ``QgsFeedback``, so the same code runs
in a QgsTask, a Processing algorithm or a test.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from osgeo import gdal, ogr, osr
from qgis.core import Qgis, QgsFeedback

from . import geometry, stac
from .catalog import MODE_RAW, Collection, Provider
from .text import plural
from .version import PLUGIN_VERSION

GDAL_HTTP_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "3",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_USERAGENT": stac.USER_AGENT.decode(),
    "VSI_CACHE": "TRUE",
    "GDAL_CACHEMAX": "512",
}
_RAW_OPTIONS = {
    **GDAL_HTTP_OPTIONS,
    "VSI_CACHE": "FALSE",
    "CPL_VSIL_CURL_CHUNK_SIZE": str(8 * 1024 * 1024),
}
_RAW_CHUNK = 8 * 1024 * 1024
_RAW_ATTEMPTS = 3
# Scenes whose headers are read when the catalogue lacks projection metadata
_MAX_HEADER_PROBES = 10
# Part of every output file name. Raise it when the same settings would now
# produce different pixels (warping, nodata, grid, band order …), so earlier
# results are not reused as if they were current.
PIPELINE_VERSION = 1

LogFn = Callable[[str, Qgis.MessageLevel], None]


@dataclass
class DownloadJob:
    provider: Provider
    collection: Collection
    items: list[dict]
    assets: list[str]
    out_dir: str
    aoi_wgs84_wkt: str  # AOI in EPSG:4326, lon/lat order
    clip_to_aoi: bool  # cut to the AOI polygon, otherwise its bounding box
    resolution_m: float
    mosaic_by_date: bool
    dst_crs_wkt: str | None = None  # None: CRS shared by most scenes
    resampling: str = "nearest"


@dataclass
class DownloadResult:
    outputs: list[str] = field(default_factory=list)
    failures: int = 0
    empty: int = 0


def safe_name(text: str, fallback: str = "item") -> str:
    """One path component from catalogue data: no separators, no "..".

    Item ids, dates and properties come from remote catalogues and end up
    in file and folder names. Ordinary ids pass unchanged, so existing
    results are still found.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return name[:150] or fallback


def group_key(item: dict, collection: Collection, mosaic_by_date: bool) -> str:
    """Output name stem; scenes sharing it are mosaicked into one file."""
    if not mosaic_by_date:
        return item["id"]
    day = (stac.item_datetime(item)[:10] or "0000-00-00").replace("-", "")
    parts = [collection.collection_id, day]
    for prop in collection.group_by:
        value = item["properties"].get(prop)
        if value is None:
            continue
        if prop == "sat:orbit_state":
            parts.append("asc" if str(value).startswith("asc") else "desc")
        elif prop.endswith("relative_orbit"):
            parts.append(f"R{int(value):03d}")
        elif prop.endswith("path"):
            parts.append(f"P{int(value):03d}")
        else:
            parts.append(str(value))
    return "_".join(parts)


_INT_RANGES = {
    gdal.GDT_Byte: (0, 255),
    gdal.GDT_Int8: (-128, 127),
    gdal.GDT_UInt16: (0, 65535),
    gdal.GDT_Int16: (-32768, 32767),
    gdal.GDT_UInt32: (0, 2**32 - 1),
    gdal.GDT_Int32: (-(2**31), 2**31 - 1),
}
FALLBACK_NODATA = -9999.0


def _fits(data_type: int, value: float) -> bool:
    """Whether ``value`` can be stored in ``data_type``; floats hold anything."""
    if data_type in _INT_RANGES:
        lo, hi = _INT_RANGES[data_type]
        return float(value).is_integer() and lo <= value <= hi
    return True


def common_nodata(declared: dict[str, float], types: dict[str, int]) -> float:
    """One nodata value for bands that declare different ones.

    A GeoTIFF stores a single nodata value. A declared value is reused only if
    no other band can hold it as real data; otherwise ``FALLBACK_NODATA``,
    for which the output type is widened with ``type_holding``.
    """
    for asset, value in declared.items():
        others = [t for a, t in types.items() if a != asset]
        if others and not any(_fits(t, value) for t in others):
            return value
    return FALLBACK_NODATA


def type_holding(data_type: int, value: float) -> int:
    """Smallest type that holds both ``data_type`` and the nodata ``value``.

    The Python bindings lack GDALDataTypeUnionWithValue, e.g. a Byte CMASK
    stacked alone still needs room for the -9999 nodata of its collection.
    """
    for candidate in (
        data_type,
        gdal.GDT_Int16,
        gdal.GDT_Int32,
        gdal.GDT_Float32,
        gdal.GDT_Float64,
    ):
        union = gdal.DataTypeUnion(data_type, candidate)
        if _fits(union, value):
            return union
    return gdal.GDT_Float64


def _has_valid_pixels(path: str) -> bool:
    ds = gdal.Open(path)
    try:
        minmax = ds.GetRasterBand(1).ComputeRasterMinMax(False, can_return_none=True)
    except RuntimeError as exc:
        # GDAL before 3.9 (QGIS 3.34) raises here instead of returning None.
        if "no valid pixels" in str(exc):
            return False
        raise
    return minmax is not None


def _catalogue_code(props: dict) -> str | None:
    """CRS of a scene from STAC projection metadata, e.g. "EPSG:32632"."""
    if props.get("proj:code"):
        return props["proj:code"]
    return f"EPSG:{props['proj:epsg']}" if props.get("proj:epsg") else None


def _majority_crs(codes: dict[str, str], fallback: str) -> str:
    """Projected CRS shared by most scenes; on a tie, ``fallback`` if it is tied.

    Geographic grids (Sentinel-1 GRD GCPs, Copernicus DEM) never win, so they
    get the metric ``fallback`` instead.
    """
    projected = Counter(c for c in codes.values() if geometry.srs(c).IsProjected())
    if not projected:
        return fallback
    top = max(projected.values())
    tied = [c for c, n in projected.items() if n == top]
    return fallback if fallback in tied else tied[0]


class Downloader:
    def __init__(self, job: DownloadJob, feedback: QgsFeedback, log: LogFn):
        self.job = job
        self.feedback = feedback
        self._log = log
        self.result = DownloadResult()
        self.signer = stac.signer_for(job.provider)
        self.src_nodata: dict[str, float | None] = {}
        self.out_nodata: float | None = None
        self.out_type = gdal.GDT_Unknown
        self.grid_origin = (0.0, 0.0)
        # Output grid, resolved in _choose_crs once scene metadata is known
        self.dst_crs_wkt = ""
        self.dst_code: str | None = None
        self.resolution = job.resolution_m
        self.aoi_bounds = (0.0, 0.0, 0.0, 0.0)
        self.cutline_wkt: str | None = None

    def canceled(self) -> bool:
        return self.feedback.isCanceled()

    def log(self, msg: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info) -> None:
        self._log(msg, level)

    def run(self) -> DownloadResult:
        os.makedirs(self.job.out_dir, exist_ok=True)
        with gdal.ExceptionMgr(useExceptions=True):
            if self.job.collection.mode == MODE_RAW:
                with gdal.config_options(_RAW_OPTIONS):
                    self._run_raw()
            else:
                with gdal.config_options(GDAL_HTTP_OPTIONS):
                    self._run_cog()
        return self.result

    # -- whole-file downloads (Sentinel-3 netCDF) -----------------------------

    def _run_raw(self):
        work = [
            (item, key)
            for item in self.job.items
            for key in self.job.assets
            if key in item["assets"]
        ]
        for n, (item, key) in enumerate(work):
            if self.canceled():
                return
            href = item["assets"][key]["href"]
            folder = os.path.join(self.job.out_dir, safe_name(item["id"]))
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(
                folder, safe_name(os.path.basename(urlparse(href).path), key)
            )
            if os.path.exists(target):
                self.log(f"Already downloaded, skipped: {target}")
            else:
                self.log(f"Downloading {item['id']} / {key}")
                try:
                    self._stream(href, target, n / len(work), 1 / len(work))
                except (RuntimeError, OSError) as exc:  # GDAL/network, disk
                    if self.canceled():
                        return
                    self.result.failures += 1
                    self.log(
                        f"{item['id']} / {key} failed: {exc}",
                        Qgis.MessageLevel.Warning,
                    )
                    continue
                if self.canceled():
                    return
            self.result.outputs.append(target)
            self.feedback.setProgress(100 * (n + 1) / len(work))

    def _stream(self, href: str, target: str, offset: float, span: float) -> None:
        """Copy a remote file in chunks into ``target``, resuming a ``.part`` file."""
        part = target + ".part"
        for attempt in range(1, _RAW_ATTEMPTS + 1):
            url = "/vsicurl/" + self.signer.sign(href, self.feedback)
            try:
                stat = gdal.VSIStatL(url)
                if stat is None:  # 404, refused connection: no exception, just None
                    raise RuntimeError(f"File not reachable: {href[:120]}")
                size = stat.size
                done = os.path.getsize(part) if os.path.exists(part) else 0
                if done > size:
                    os.remove(part)
                    done = 0
                src = gdal.VSIFOpenL(url, "rb")
                try:
                    gdal.VSIFSeekL(src, done, 0)
                    with open(part, "ab") as out:
                        while done < size:
                            if self.canceled():
                                return  # keep the .part file for resuming
                            chunk = gdal.VSIFReadL(1, min(_RAW_CHUNK, size - done), src)
                            if not chunk:
                                raise RuntimeError("Connection interrupted")
                            out.write(chunk)
                            done += len(chunk)
                            self.feedback.setProgress(
                                100 * (offset + span * done / size)
                            )
                finally:
                    gdal.VSIFCloseL(src)
                os.replace(part, target)
                return
            except RuntimeError:
                if attempt == _RAW_ATTEMPTS or self.canceled():
                    raise
                self.signer.invalidate()
                self.log(f"Download interrupted, resuming (attempt {attempt + 1})")

    # -- windowed COG reads -----------------------------------------------------

    def _groups(self) -> OrderedDict[str, list[dict]]:
        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for item in sorted(self.job.items, key=stac.item_datetime):
            key = group_key(item, self.job.collection, self.job.mosaic_by_date)
            groups.setdefault(key, []).append(item)
        return groups

    def _source(self, item: dict, asset: str) -> str:
        """GDAL path of a remote asset, signed where the provider needs it."""
        return "/vsicurl/" + self.signer.sign(
            item["assets"][asset]["href"], self.feedback
        )

    def _open(self, item: dict, asset: str) -> gdal.Dataset:
        return gdal.Open(self._source(item, asset))

    def _resampling_for(self, asset: str) -> tuple[str, str | None]:
        """(resampleAlg, overviewLevel) for one asset.

        Class and flag bands are never interpolated and never read from COG
        overviews, which are usually averaged and contain classes that do not
        exist. Aggregating them to a coarser grid uses the majority class.
        Measured values keep the chosen method and may use overviews.
        """
        c = self.job.collection
        meta = next(
            (it["assets"][asset] for it in self.job.items if asset in it["assets"]),
            None,
        )
        if c.is_categorical(asset, meta):
            coarser = self.job.resolution_m > c.native_res
            return ("mode" if coarser else "near"), "NONE"
        return self.job.resampling, None

    def _choose_crs(self) -> dict[str, str]:
        """Resolve output CRS, resolution and AOI geometry in that CRS.

        Without an explicit CRS the one shared by most scenes is used, so their
        pixels are copied instead of resampled. Otherwise the AOI decides: UTM,
        polar stereographic beyond 84° N / 80° S, equal-area for very wide
        areas. Returns the known CRS per scene id.
        """
        job = self.job
        aoi = geometry.from_geojson(
            geometry.unwrap_antimeridian(
                geometry.to_geojson(ogr.CreateGeometryFromWkt(job.aoi_wgs84_wkt))
            )
        )
        codes: dict[str, str] = {}
        if job.dst_crs_wkt:
            dst = geometry.srs(job.dst_crs_wkt)
            self.dst_code = geometry.auth_code(dst)
        else:
            codes = self._scene_codes()
            self.dst_code = _majority_crs(codes, geometry.auto_crs(aoi))
            dst = geometry.srs(self.dst_code)
        self._set_grid(dst, aoi)
        self._log_target(dst, aoi, codes)
        return codes

    def _scene_codes(self) -> dict[str, str]:
        """CRS per scene id: from the catalogue, else from a few file headers."""
        codes = {
            item["id"]: code
            for item in self.job.items
            if (code := _catalogue_code(item["properties"]))
        }
        if codes:
            return codes
        # Catalogues without projection metadata (INPE)
        asset = self.job.assets[0]
        probe = [it for it in self.job.items if asset in it["assets"]]
        for item in probe[:_MAX_HEADER_PROBES]:
            ds = self._open(item, asset)
            ref = ds.GetSpatialRef() if ds.GetGCPCount() == 0 else None
            code = geometry.auth_code(ref) if ref is not None else None
            if code:
                codes[item["id"]] = code
        return codes

    def _set_grid(self, dst: osr.SpatialReference, aoi: ogr.Geometry) -> None:
        """Output CRS, pixel size and the AOI (bounds and cutline) in that CRS."""
        self.dst_crs_wkt = dst.ExportToWkt()
        self.resolution = geometry.resolution_in_crs(self.job.resolution_m, dst)
        projected_aoi = geometry.project(aoi, dst)
        xmin, xmax, ymin, ymax = projected_aoi.GetEnvelope()
        self.aoi_bounds = (xmin, ymin, xmax, ymax)
        self.cutline_wkt = projected_aoi.ExportToWkt() if self.job.clip_to_aoi else None

    def _log_target(
        self, dst: osr.SpatialReference, aoi: ogr.Geometry, codes: dict[str, str]
    ) -> None:
        self.log(self._target_message(dst, aoi))
        for asset in self.job.assets:
            alg, _ = self._resampling_for(asset)
            if alg != self.job.resampling:
                self.log(f"{asset}: class band, resampled with {alg}.")
        code = self.dst_code
        other = sum(1 for c in codes.values() if code and c != code)
        if other and other == len(self.job.items):
            self.log("No scene is in the target CRS; all pixels are resampled.")
        elif other:
            self.log(f"{plural(other, 'scene')} in another CRS will be resampled.")

    def _target_message(self, dst: osr.SpatialReference, aoi: ogr.Geometry) -> str:
        code, res = self.dst_code, self.job.resolution_m
        name = code if code and ":" in code else dst.GetName()
        if not dst.IsGeographic():
            return f"Target CRS: {name}, resolution {res:g} m"
        lat = aoi.Centroid().GetY()
        ew = geometry.east_west_metres(res, lat)
        hemisphere = "N" if lat >= 0 else "S"
        return (
            f"Target CRS: {name}, {res:g} m north–south, "
            f"{ew:.3g} m east–west at {abs(lat):.0f}° {hemisphere}"
        )

    def _probe_sources(self, codes: dict[str, str]) -> None:
        """Read headers for data type, declared nodata and pixel grid origin.

        Files that declare no nodata (S2 L2A, CBERS CMASK) fall back to the
        catalogue value, so mosaic collars never overwrite valid pixels. All
        bands are written with one common type and nodata value, because a
        GeoTIFF stores a single nodata value for all of its bands.
        """
        types = self._read_band_headers()
        if not self._find_grid_origin(codes) and not codes:
            # With codes known, _choose_crs has already reported this.
            self.log("No scene is in the target CRS; all pixels are resampled.")
        out_type = functools.reduce(gdal.DataTypeUnion, types.values())
        self.out_nodata = self._output_nodata(types)
        if self.out_nodata is not None:
            out_type = type_holding(out_type, self.out_nodata)
        self.out_type = out_type

    def _read_band_headers(self) -> dict[str, int]:
        """Data type per asset; fills ``src_nodata`` (declared, else catalogue)."""
        c = self.job.collection
        types: dict[str, int] = {}
        for asset in self.job.assets:
            item = next((it for it in self.job.items if asset in it["assets"]), None)
            if item is None:
                continue
            ds = self._open(item, asset)
            band = ds.GetRasterBand(1)
            types[asset] = band.DataType
            declared = band.GetNoDataValue()
            self.src_nodata[asset] = (
                declared if declared is not None else c.nodata_for(asset)
            )
            ds = None
        return types

    def _find_grid_origin(self, codes: dict[str, str]) -> bool:
        """Take the pixel grid origin from a scene in the target CRS, if any."""
        dst = geometry.srs(self.dst_crs_wkt)
        asset = self.job.assets[0]
        candidates = [it for it in self.job.items if asset in it["assets"]]
        preferred = [it for it in candidates if codes.get(it["id"]) == self.dst_code]
        for item in (preferred or candidates)[:_MAX_HEADER_PROBES]:
            ds = self._open(item, asset)
            ref = ds.GetSpatialRef()
            if ds.GetGCPCount() == 0 and ref is not None and ref.IsSame(dst):
                gt = ds.GetGeoTransform()
                self.grid_origin = (gt[0], gt[3])
                return True
        return False

    def _output_nodata(self, types: dict[str, int]) -> float | None:
        """The single nodata value written to every band, None if none is known."""
        c = self.job.collection
        if c.nodata is not None:
            return c.nodata
        declared = {a: v for a, v in self.src_nodata.items() if v is not None}
        values = set(declared.values())
        if len(values) == 1:
            return values.pop()
        if values:
            # Each band keeps its own srcNodata; only the output value is shared.
            return common_nodata(declared, types)
        if self.cutline_wkt:
            self.log(
                "No nodata value known: pixels outside the area are 0.",
                Qgis.MessageLevel.Warning,
            )
        return None

    def _params(self, items: list[dict], bounds) -> dict:
        job = self.job
        return {
            "pipeline": PIPELINE_VERSION,
            "provider": job.provider.stac_url,
            "collection": job.collection.collection_id,
            "items": sorted(it["id"] for it in items),
            "assets": job.assets,
            "crs": self.dst_code,
            "crs_wkt": self.dst_crs_wkt,
            "bounds": [round(v, 6) for v in bounds],
            "resolution": self.resolution,
            "resampling": {a: self._resampling_for(a)[0] for a in job.assets},
            "cutline_wkt": self.cutline_wkt,
            "nodata": self.out_nodata,
            "data_type": gdal.GetDataTypeName(self.out_type),
        }

    def _run_cog(self):
        self._check_assets_offered()
        groups = self._groups()
        n_assets = len(self.job.assets)
        steps = max(1, len(groups) * n_assets)
        self._probe_sources(self._choose_crs())
        bounds = geometry.snap_bounds(
            self.aoi_bounds, self.resolution, self.grid_origin
        )
        for g, (name, items) in enumerate(groups.items()):
            if self.canceled():
                return
            done = g * n_assets
            self.feedback.setProgress(100 * done / steps)
            if self._missing_assets(name, items):
                continue
            span = n_assets / steps  # share of the progress bar for this group
            if not self._download_group(name, items, bounds, done / steps, span):
                return  # cancelled
        self.feedback.setProgress(100)

    def _check_assets_offered(self) -> None:
        available = {a for it in self.job.items for a in it["assets"]}
        missing = [a for a in self.job.assets if a not in available]
        if missing:
            offered = sorted(available & set(self.job.collection.assets))
            raise ValueError(
                f"None of the scenes contains {', '.join(missing)}. "
                f"Available: {', '.join(offered) or 'no matching bands'}."
            )

    def _missing_assets(self, name: str, items: list[dict]) -> bool:
        missing = [
            a for a in self.job.assets if not any(a in it["assets"] for it in items)
        ]
        if missing:
            self.log(
                f"{name}: assets missing ({', '.join(missing)}), skipped.",
                Qgis.MessageLevel.Warning,
            )
        return bool(missing)

    def _target(self, name: str, params: dict) -> str:
        """Output path; the parameter hash keeps results of different settings apart.

        An existing file is reused only when it is exactly what this run would
        produce. Only tells settings apart; not a security use of the hash.
        """
        digest = hashlib.sha1(
            json.dumps(params, sort_keys=True).encode(), usedforsecurity=False
        ).hexdigest()[:8]
        return os.path.join(self.job.out_dir, f"{safe_name(name)}_{digest}.tif")

    def _download_group(
        self, name: str, items: list[dict], bounds, offset: float, span: float
    ) -> bool:
        """Write one output file for a group of scenes; False if cancelled."""
        params = self._params(items, bounds)
        target = self._target(name, params)
        if os.path.exists(target):
            self.log(f"Already downloaded, skipped: {os.path.basename(target)}")
            self.result.outputs.append(target)
            return True
        n_assets = len(self.job.assets)
        self.log(f"{name}: {plural(len(items), 'scene')}, {plural(n_assets, 'band')}")
        stem = os.path.splitext(os.path.basename(target))[0]
        band_files = [
            os.path.join(self.job.out_dir, f".{stem}_{safe_name(a)}.tmp.tif")
            for a in self.job.assets
        ]
        try:
            outcome = self._warp_bands(name, items, band_files, bounds, offset, span)
            if outcome == "cancelled":
                return False
            if outcome == "done":
                self._stack(band_files, target, items)
                self._write_sidecar(target, params)
                self.result.outputs.append(target)
                self.log(f"Done: {os.path.basename(target)}")
        except (RuntimeError, OSError, ValueError) as exc:  # GDAL, disk, data
            if self.canceled():
                return False
            self.result.failures += 1
            self.log(f"{name} failed: {exc}", Qgis.MessageLevel.Warning)
        finally:
            for f in band_files + [target + ".part.tif"]:
                if os.path.exists(f):
                    os.remove(f)
        return True

    def _warp_bands(
        self,
        name: str,
        items: list[dict],
        band_files: list[str],
        bounds,
        offset: float,
        span: float,
    ) -> str:
        """Warp every asset to its temporary file: "done", "empty" or "cancelled".

        An area without valid pixels in the first band stops early ("empty").
        """
        step = span / len(self.job.assets)
        for b, (asset, tmp) in enumerate(zip(self.job.assets, band_files, strict=True)):
            self._warp(items, asset, tmp, bounds, offset + b * step, step)
            if self.canceled():
                return "cancelled"
            if b == 0 and not _has_valid_pixels(tmp):
                self.log(
                    f"{name}: no valid pixels in the area, skipped.",
                    Qgis.MessageLevel.Warning,
                )
                self.result.empty += 1
                return "empty"
        return "done"

    def _write_sidecar(self, target: str, params: dict) -> None:
        """Settings (as hashed into the name) plus when and with what it was made."""
        sidecar = os.path.splitext(target)[0] + ".json"
        made_with = {
            "created": datetime.now(timezone.utc).isoformat(),
            "plugin_version": PLUGIN_VERSION,
            "gdal_version": gdal.__version__,
        }
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump({**params, **made_with}, fh, indent=2)

    def _warp(
        self,
        items: list[dict],
        asset: str,
        dst: str,
        bounds,
        offset: float,
        span: float,
    ):
        def progress(frac, _msg, _data):
            self.feedback.setProgress(100 * (offset + frac * span))
            return 0 if self.canceled() else 1

        kwargs = self._warp_options(asset, bounds)
        kwargs["callback"] = progress
        cutline = geometry.cutline_options(
            self.cutline_wkt, self.dst_crs_wkt if self.cutline_wkt else None
        )
        with cutline as cutline_kwargs:
            kwargs.update(cutline_kwargs)
            for attempt in (1, 2):
                sources = [
                    self._source(it, asset) for it in items if asset in it["assets"]
                ]
                try:
                    ds = gdal.Warp(dst, sources, **kwargs)
                    if ds is None:
                        raise RuntimeError(gdal.GetLastErrorMsg() or "gdal.Warp failed")
                    ds = None
                    return
                except RuntimeError as exc:
                    # SAS tokens expire after ~45 min; a long read then fails with 403.
                    if attempt == 1 and "403" in str(exc) and not self.canceled():
                        self.signer.invalidate()
                        self.log("Access token expired; got a new one.")
                        continue
                    raise

    def _warp_options(self, asset: str, bounds) -> dict:
        """gdal.Warp options for one asset on the shared output grid."""
        resample_alg, overview_level = self._resampling_for(asset)
        optional = {
            "srcNodata": self.src_nodata.get(asset),
            "dstNodata": self.out_nodata,
            "overviewLevel": overview_level,
        }
        return {
            "format": "GTiff",
            "dstSRS": self.dst_crs_wkt,
            "outputBounds": bounds,
            "xRes": self.resolution,
            "yRes": self.resolution,
            "outputType": self.out_type,
            "resampleAlg": resample_alg,
            "multithread": True,
            "warpOptions": ["NUM_THREADS=ALL_CPUS"],
            "creationOptions": ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
            **{k: v for k, v in optional.items() if v is not None},
        }

    def _stack(self, band_files: list[str], target: str, items: list[dict]):
        part = target + ".part.tif"
        vrt_path = f"/vsimem/{os.path.basename(target)}.vrt"
        # Close to flush the VRT before reading it
        gdal.BuildVRT(vrt_path, band_files, separate=True).Close()
        ds = gdal.Translate(
            part,
            vrt_path,
            format="GTiff",
            creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
        )
        # Multi-band assets (NAIP RGBN, ASTER VNIR) contribute all their bands.
        band = 1
        for asset, path in zip(self.job.assets, band_files, strict=True):
            src = gdal.Open(path)
            count = src.RasterCount
            src = None
            for n in range(1, count + 1):
                name = asset if count == 1 else f"{asset}_{n}"
                ds.GetRasterBand(band).SetDescription(name)
                band += 1
        ds.SetMetadataItem("STAC_COLLECTION", self.job.collection.collection_id)
        ds.SetMetadataItem("STAC_ITEMS", ",".join(it["id"] for it in items))
        ds = None
        gdal.Unlink(vrt_path)
        os.replace(part, target)
