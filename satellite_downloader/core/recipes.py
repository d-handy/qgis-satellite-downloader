"""Purpose-based recipes: pick the right data for a place without research.

A recipe names what the user wants ("current image", "radar", "elevation").
``plan`` searches the sources, groups scenes into acquisitions, checks AOI
coverage and clear sky, widens the time window when needed and explains from
facts when nothing fits. ``run`` downloads the chosen acquisition and derives
the recipe's products.
"""

from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

from osgeo import gdal, ogr
from qgis.core import Qgis, QgsFeedback

from . import clouds, geometry, places, postprocess, stac
from .catalog import COLLECTIONS_BY_KEY, PROVIDERS, SEARCH_STATIC, Collection
from .pipeline import GDAL_HTTP_OPTIONS, Downloader, DownloadJob, LogFn, group_key
from .text import LOG_HINT, or_join

MAX_OUTPUT_PIXELS = 10_000  # long side of a recipe output
STEP_LABELS = {
    "db": "backscatter in dB",
    "hillshade": "hillshade",
    "slope": "slope",
}
_CLEAR_BATCH = 6
_MAX_CLEAR_CHECKS = 18  # per time window


@dataclass(frozen=True)
class Recipe:
    key: str
    label: str
    description: str
    sources: tuple[str, ...]  # collection keys; the newest suitable acquisition wins
    bands: tuple[str, ...] = ()  # common band names; empty = collection default
    windows_days: tuple[int, ...] = (30, 90, 365)
    min_clear: float | None = None  # % clear sky inside the AOI
    min_coverage: float = 95.0  # % of the AOI inside the acquisition
    static: bool = False  # no time filter (e.g. elevation models)
    style: str = "rgb"  # rgb | sar | dem
    postprocess: tuple[str, ...] = ()
    # For measured values; class and flag bands always use nearest neighbour
    resampling: str = "nearest"


RECIPES: list[Recipe] = [
    Recipe(
        "current",
        "Current image",
        "Most recent acquisition with clear sky over the area, in true colour. "
        "Sentinel-2 (10 m) and Landsat 8/9 (30 m) are searched together.",
        ("s2-l2a", "landsat-c2-l2"),
        ("red", "green", "blue"),
        min_clear=95.0,
    ),
    Recipe(
        "radar",
        "Radar (through clouds, day and night)",
        "Most recent Sentinel-1 acquisition (10 m), backscatter in dB. Radar sees "
        "through clouds and needs no daylight.",
        ("s1-rtc",),
        windows_days=(12, 30, 90),
        style="sar",
        postprocess=("db",),
    ),
    Recipe(
        "dem",
        "Elevation and terrain",
        "Copernicus DEM (30 m) with hillshade and slope.",
        ("cop-dem-30",),
        windows_days=(),
        static=True,
        style="dem",
        postprocess=("hillshade", "slope"),
        # The DEM is reprojected from lon/lat; nearest neighbour would repeat
        # and drop rows, which shows as stripes in hillshade and slope.
        resampling="bilinear",
    ),
]
RECIPES_BY_KEY = {r.key: r for r in RECIPES}


class NoDataError(RuntimeError):
    """Nothing suitable was found; the message explains why."""


@dataclass
class Acquisition:
    collection: Collection
    items: list[dict]
    coverage: float | None
    clear: float | None = None

    @property
    def when(self) -> str:
        return max(stac.item_datetime(i) for i in self.items)

    @property
    def key(self) -> str:
        return group_key(self.items[0], self.collection, True)


@dataclass
class RecipePlan:
    recipe: Recipe
    acquisition: Acquisition
    assets: list[str]
    resolution_m: float
    aoi_wkt: str
    notes: list[str] = field(default_factory=list)


@dataclass
class RecipeResult:
    plan: RecipePlan
    outputs: list[str]  # main product first
    derived: dict[str, str]  # postprocess step -> path
    attribution: str


# -- facts for explanations --------------------------------------------------------


def noon_sun_elevation(lat: float, day: date) -> float:
    """Approximate solar elevation at local noon (degrees)."""
    declination = -23.44 * math.cos(
        math.radians(360 / 365 * (day.timetuple().tm_yday + 10))
    )
    return 90 - abs(lat - declination)


def next_daylight(lat: float, today: date, min_elevation: float = 10.0) -> date | None:
    for offset in range(367):
        day = today + timedelta(days=offset)
        if noon_sun_elevation(lat, day) >= min_elevation:
            return day
    return None


# -- planning ----------------------------------------------------------------------


def _coverage(items: list[dict], aoi_split: ogr.Geometry) -> float | None:
    union = None
    for item in items:
        if not item.get("geometry"):
            return None
        footprint = geometry.from_geojson(item["geometry"])
        if not footprint.IsValid():
            footprint = footprint.MakeValid()
        union = footprint if union is None else union.Union(footprint)
    return (
        geometry.aoi_coverage(geometry.to_geojson(union), aoi_split) if union else None
    )


def _assets(
    recipe: Recipe, collection: Collection, items: list[dict]
) -> list[str] | None:
    """Assets present in every item of an acquisition, or None."""
    if recipe.bands:
        wanted = collection.assets_for(recipe.bands)
        options = [wanted] if wanted else []
    elif collection.key.startswith("s1-"):
        options = [["vv", "vh"], ["hh", "hv"], ["vv"], ["hh"]]
    else:
        options = [list(collection.default_assets)]
    for assets in options:
        if all(a in it["assets"] for it in items for a in assets):
            return assets
    return None


def _acquisitions(
    recipe: Recipe,
    aoi_geojson: dict,
    aoi_split: ogr.Geometry,
    start: date,
    end: date,
    feedback,
    unreachable: dict[str, str],
) -> list[Acquisition]:
    """Acquisitions of all sources that answer, newest first.

    A source whose catalogue fails is recorded in ``unreachable`` (key ->
    error) and skipped from then on: its retries already took their time, and
    the other sources may still have what the recipe needs.
    """
    found: list[Acquisition] = []
    for key in recipe.sources:
        if key in unreachable:
            continue
        c = COLLECTIONS_BY_KEY[key]
        # Scene-wide cloud cover only as a coarse pre-filter; the AOI decides.
        max_cloud = 90.0 if recipe.min_clear is not None and c.has_cloud_cover else None
        try:
            items, _ = stac.search(
                PROVIDERS[c.provider],
                c,
                aoi_geojson,
                start.isoformat(),
                end.isoformat(),
                max_cloud,
                60 if not recipe.static else 50,
                feedback,
            )
        except stac.StacError as exc:
            if feedback is not None and feedback.isCanceled():
                raise
            unreachable[key] = str(exc)
            continue
        groups: dict[str, list[dict]] = {}
        for item in items:
            groups.setdefault(group_key(item, c, True), []).append(item)
        for group in groups.values():
            if _assets(recipe, c, group) is not None:
                found.append(Acquisition(c, group, _coverage(group, aoi_split)))
    # Newest first; on the same day the source listed first (higher resolution) wins.
    order = {k: n for n, k in enumerate(recipe.sources)}
    found.sort(key=lambda a: (a.when[:10], -order[a.collection.key]), reverse=True)
    return found


Signer = stac.PlanetaryComputerSigner | stac.NullSigner


def _check_clear(
    acquisitions: list[Acquisition],
    aoi_unwrapped: ogr.Geometry,
    feedback,
    signers: dict[str, Signer],
) -> None:
    """Fill ``clear`` in parallel; ``signers`` (provider key -> signer) is shared.

    A signer caches its access tokens, so one per provider serves every
    acquisition of a plan. Missing signers are created here, before the
    workers start, so no two threads create one for the same provider.
    """

    def work(acq: Acquisition):
        c = acq.collection
        with (
            gdal.ExceptionMgr(useExceptions=True),
            gdal.config_options(GDAL_HTTP_OPTIONS),
        ):
            try:
                acq.clear = clouds.clear_percent(
                    acq.items, c, aoi_unwrapped, signers[c.provider], feedback
                )
            except RuntimeError:
                acq.clear = None

    todo = [a for a in acquisitions if a.clear is None]
    for acq in todo:
        provider = acq.collection.provider
        if provider not in signers:
            signers[provider] = stac.signer_for(PROVIDERS[provider])
    with ThreadPoolExecutor(max_workers=_CLEAR_BATCH) as pool:
        list(pool.map(work, todo))


def _resolution(collection: Collection, area_side_m: float) -> float:
    native = collection.native_res
    needed = area_side_m / MAX_OUTPUT_PIXELS
    if needed <= native:
        return native
    return native * math.ceil(needed / native)  # multiples keep the pixel grid aligned


def _score(acq: Acquisition) -> float:
    """Clear share of the whole area (%): clear sky times coverage."""
    return (acq.clear or 0) * (acq.coverage or 0) / 100


def _side_m(aoi_unwrapped: ogr.Geometry) -> float:
    """Longer side of the AOI in metres, in the CRS a download would use."""
    ref = geometry.srs(geometry.auto_crs(aoi_unwrapped))
    xmin, xmax, ymin, ymax = geometry.project(aoi_unwrapped, ref).GetEnvelope()
    return max(xmax - xmin, ymax - ymin)


def _candidates(
    found: list[Acquisition], min_coverage: float
) -> tuple[list[Acquisition], bool]:
    """(acquisitions worth choosing from, whether they cover the whole area).

    Without one that covers ``min_coverage`` % of the area, the five with
    the largest coverage are the candidates.
    """
    covering = [a for a in found if (a.coverage or 0) >= min_coverage]
    if covering:
        return covering, True
    return sorted(found, key=lambda a: a.coverage or 0, reverse=True)[:5], False


class _Planner:
    """Search the time windows of a recipe, newest acquisitions first.

    Cloud checks are remembered across windows, so a wider window does not
    check the same acquisition again, and the clearest one seen so far is
    the fallback when none reaches ``min_clear``.
    """

    def __init__(self, recipe: Recipe, aoi_wkt: str, today: date, feedback, say):
        self.recipe, self.today, self.feedback, self.say = recipe, today, feedback, say
        self.aoi_geojson = geometry.unwrap_antimeridian(
            geometry.to_geojson(ogr.CreateGeometryFromWkt(aoi_wkt))
        )
        self.aoi_unwrapped = geometry.from_geojson(self.aoi_geojson)
        _, self.aoi_split = stac.search_geometry(self.aoi_geojson)
        self.notes: list[str] = []
        self.checked: dict[str, float | None] = {}
        self.best: Acquisition | None = None
        self.unreachable: dict[str, str] = {}  # collection key -> error
        self.signers: dict[str, Signer] = {}  # provider key -> signer, see _check_clear

    def choose(self) -> Acquisition:
        chosen = self._choose()
        if self.unreachable:
            self.notes.append(self._unreachable_note())
        return chosen

    def _choose(self) -> Acquisition:
        windows = self.recipe.windows_days or (0,)
        for days in windows:
            if self.feedback and self.feedback.isCanceled():
                raise NoDataError("Cancelled")
            chosen = self._search_window(days)
            if chosen:
                return chosen
        if self.best is None:
            lat = self.aoi_unwrapped.Centroid().GetY()
            reason = explain_missing(self.recipe, lat, self.today)
            if self.unreachable:
                reason += " " + self._unreachable_note()
            raise NoDataError(reason)
        self.notes.append(
            f"No acquisition in the last {windows[-1]} days had at least "
            f"{self.recipe.min_clear:g} % clear sky over the area. Using the clearest: "
            f"{self.best.when[:10]} with {self.best.clear or 0:.0f} % clear. "
            'Where clouds are frequent, the "Radar" recipe helps.'
        )
        return self.best

    def _search_window(self, days: int) -> Acquisition | None:
        recipe = self.recipe
        if recipe.static:
            self.say("Searching tiles …")
        else:
            self.say(f"Searching acquisitions from the last {days} days …")
        start = self.today - timedelta(days=days)
        known_failures = set(self.unreachable)
        found = _acquisitions(
            recipe,
            self.aoi_geojson,
            self.aoi_split,
            start,
            self.today,
            self.feedback,
            self.unreachable,
        )
        for key in self.unreachable.keys() - known_failures:
            error = self.unreachable[key][:120]
            self.say(
                f"{COLLECTIONS_BY_KEY[key].label}: catalogue not reachable ({error})"
            )
        if len(self.unreachable) == len(recipe.sources):
            raise NoDataError(
                f"Could not reach {or_join(self._unreachable_labels())}. Try again later. "
                f"{LOG_HINT}"
            )
        if not found:
            return None
        pool, covering = _candidates(found, recipe.min_coverage)
        if recipe.min_clear is not None:
            return self._first_clear(pool, recipe.min_clear)
        if not covering:
            self.notes.append(
                "No acquisition covers the whole area; using the one with the "
                f"largest coverage ({pool[0].coverage or 0:.0f} %)."
            )
        return pool[0]

    def _unreachable_labels(self) -> list[str]:
        return [COLLECTIONS_BY_KEY[k].label for k in self.unreachable]

    def _unreachable_note(self) -> str:
        labels = self._unreachable_labels()
        left_out = "it was" if len(labels) == 1 else "they were"
        return (
            f"{or_join(labels, 'and')} could not be reached, so {left_out} left out; "
            "a later attempt may find newer data."
        )

    def _first_clear(
        self, pool: list[Acquisition], min_clear: float
    ) -> Acquisition | None:
        """Newest acquisition with at least ``min_clear`` % clear sky, checked in batches."""
        for acq in pool:
            acq.clear = self.checked.get(acq.key, acq.clear)
        candidates = pool[:_MAX_CLEAR_CHECKS]
        for n in range(0, len(candidates), _CLEAR_BATCH):
            batch = candidates[n : n + _CLEAR_BATCH]
            self.say(
                f"Checking clouds in the area ({n + len(batch)} of {len(candidates)}) …"
            )
            _check_clear(batch, self.aoi_unwrapped, self.feedback, self.signers)
            for acq in batch:
                self.checked[acq.key] = acq.clear
                if self.best is None or _score(acq) > _score(self.best):
                    self.best = acq
            chosen = next((a for a in batch if (a.clear or 0) >= min_clear), None)
            if chosen:
                return chosen
        return None


def plan(
    recipe: Recipe,
    aoi_wkt: str,
    today: date,
    feedback: QgsFeedback | None = None,
    log: LogFn | None = None,
) -> RecipePlan:
    def say(msg):
        if log:
            log(msg, Qgis.MessageLevel.Info)

    planner = _Planner(recipe, aoi_wkt, today, feedback, say)
    chosen = planner.choose()
    notes = planner.notes
    side_m = _side_m(planner.aoi_unwrapped)
    assets = _assets(recipe, chosen.collection, chosen.items) or []
    resolution = _resolution(chosen.collection, side_m)
    native = chosen.collection.native_res
    if resolution > native:
        notes.append(
            f"Large area ({side_m / 1000:.0f} km): resolution automatically coarsened "
            f"to {resolution:g} m (native {native:g} m)."
        )
    partial = chosen.coverage is not None and chosen.coverage < recipe.min_coverage
    if partial and not notes:
        notes.append(f"The acquisition covers {chosen.coverage:.0f} % of the area.")
    return RecipePlan(recipe, chosen, assets, resolution, aoi_wkt, notes)


def explain_missing(recipe: Recipe, lat: float, today: date) -> str:
    """Fact-based reason why a recipe found nothing."""
    if recipe.static:
        return "There are no elevation model tiles for this area."
    span = recipe.windows_days[-1]
    if recipe.min_clear is not None:
        if noon_sun_elevation(lat, today) < 5:
            back = next_daylight(lat, today)
            when = f" from about {back:%Y-%m-%d}" if back else ""
            return (
                "Polar night: the sun stays below the horizon here at noon. Optical "
                f'acquisitions resume{when}. The "Radar" recipe works now as well.'
            )
        return (
            f"There are no optical Sentinel-2 or Landsat acquisitions here in the last "
            f"{span} days. These satellites do not image the open sea or the interior "
            'of Antarctica; the "Radar" recipe covers land and coasts worldwide.'
        )
    return (
        f"There is no Sentinel-1 acquisition here in the last {span} days. "
        "Sentinel-1 images the open sea only irregularly."
    )


# -- running -------------------------------------------------------------------------


def slug(text: str) -> str:
    """Folder name for a place, see ``places.short_name``."""
    text = places.short_name(text).lower()
    text = (
        text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    )
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "area"


_collection_info: dict[str, dict] = {}


def _info(collection: Collection, feedback) -> dict:
    """STAC collection document, cached; static catalogues have no API to ask."""
    info = _collection_info.get(collection.collection_id)
    if info is not None:
        return info
    provider = PROVIDERS[collection.provider]
    if provider.search_method == SEARCH_STATIC:
        return {"license": collection.license}
    try:
        info = stac.get_json(
            f"{provider.stac_url}/collections/{collection.collection_id}",
            feedback=feedback,
        )
    except stac.StacError:
        info = {}
    _collection_info[collection.collection_id] = info
    return info


def _licence(info: dict) -> str | None:
    if not info.get("license"):
        return None
    # "or []": catalogues may send null instead of leaving the key out.
    links = info.get("links") or []
    link = next((ln["href"] for ln in links if ln.get("rel") == "license"), "")
    return f"Licence: {info['license']}" + (f" ({link})" if link else "")


def attribution(collection: Collection, when: str, feedback=None) -> str:
    """Source and licence text from the STAC collection, plus Copernicus notice."""
    info = _info(collection, feedback)
    parts = []
    if collection.collection_id.lower().startswith("sentinel"):
        parts.append(f"Contains modified Copernicus Sentinel data {when[:4]}")
    names = [p.get("name") for p in info.get("providers") or [] if p.get("name")]
    if names:
        parts.append("Source: " + ", ".join(dict.fromkeys(names)))
    if licence := _licence(info):
        parts.append(licence)
    parts.append(f"Provided via {PROVIDERS[collection.provider].label}")
    return " · ".join(parts)


def run(
    recipe_plan: RecipePlan,
    out_root: str,
    place_name: str,
    feedback: QgsFeedback,
    log: LogFn,
    status: LogFn | None = None,
) -> RecipeResult:
    """Download and derive; ``log`` gets the details, ``status`` what users follow."""
    recipe, acq = recipe_plan.recipe, recipe_plan.acquisition
    out_dir = os.path.join(out_root, slug(place_name), recipe.key)
    job = DownloadJob(
        PROVIDERS[acq.collection.provider],
        acq.collection,
        acq.items,
        recipe_plan.assets,
        out_dir,
        recipe_plan.aoi_wkt,
        True,
        recipe_plan.resolution_m,
        True,
        resampling=recipe.resampling,
    )
    downloader = Downloader(job, feedback, log)
    result = downloader.run()
    if not result.outputs:
        if result.empty:
            raise NoDataError("The acquisition has no valid pixels in the area.")
        raise NoDataError(f"The download failed. {LOG_HINT}")
    main = result.outputs[0]
    derived = {}
    with gdal.ExceptionMgr(useExceptions=True):
        for step in recipe.postprocess:
            (status or log)(f"Computing {STEP_LABELS[step]} …", Qgis.MessageLevel.Info)
            derived[step] = postprocess.STEPS[step](main)
    return RecipeResult(
        recipe_plan,
        result.outputs,
        derived,
        attribution(acq.collection, acq.when, feedback),
    )
