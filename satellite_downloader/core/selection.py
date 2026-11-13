"""Search many collections at once: pre-filter, parallel search, merged results.

A category ("Radar", "Optical", ...) can hold dozens of collections. Most are
ruled out without a network request: their spatial extent misses the area or
their data period misses the dates. The rest are searched in parallel and the
items are tagged with their collection, so one table can list scenes of
different sensors side by side.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from qgis.core import QgsFeedback

from . import stac
from .catalog import COLLECTIONS_BY_KEY, PROVIDERS, Collection

BBox = tuple[float, float, float, float]  # west, south, east, north
COLLECTION_KEY = "satellite_downloader:collection"
UID_KEY = "satellite_downloader:uid"
_WORKERS = 6


def covers(collection: Collection, area: BBox | None) -> bool:
    """True if the collection's extent may intersect the area (unknown = True)."""
    if area is None or not collection.bbox or len(collection.bbox) < 4:
        return True
    w, s, e, n = collection.bbox[:4]
    # Some catalogues swap south and north (Planetary Computer: ALOS PALSAR
    # [-180, 85, 180, -56]); a swapped extent must not exclude the whole world.
    s, n = min(s, n), max(s, n)
    aw, as_, ae, an = area
    return not (ae < w or aw > e or an < s or as_ > n)


def overlaps_period(collection: Collection, start: str, end: str) -> bool:
    """True if the collection may have data between the ISO dates (unknown = True)."""
    if not collection.time_filter:
        return True  # static products such as elevation models
    first, last = collection.interval
    if last and last[:10] < start:
        return False
    return not (first and first[:10] > end)


def prefilter(
    collections: list[Collection], area: BBox | None, start: str, end: str
) -> tuple[list[Collection], list[tuple[Collection, str]]]:
    """(collections worth searching, [(skipped collection, reason)])."""
    keep, skipped = [], []
    for c in collections:
        if not covers(c, area):
            skipped.append((c, "area outside coverage"))
        elif not overlaps_period(c, start, end):
            skipped.append((c, "no data in the period"))
        else:
            keep.append(c)
    return keep, skipped


def item_collection(item: dict) -> Collection:
    return COLLECTIONS_BY_KEY[item[COLLECTION_KEY]]


def tag(item: dict, collection: Collection) -> dict:
    item[COLLECTION_KEY] = collection.key
    item[UID_KEY] = f"{collection.key}|{item['id']}"
    return item


@dataclass
class SearchOutcome:
    items: list[dict] = field(default_factory=list)
    discarded: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # collection key -> message
    counts: dict[str, int] = field(default_factory=dict)  # collection key -> items


def limited(outcome: SearchOutcome, max_items_each: int) -> list[str]:
    """Keys of collections that reached the limit, so more scenes may exist."""
    return [key for key, n in outcome.counts.items() if n >= max_items_each]


def _or_join(parts: list[str]) -> str:
    """ "a", "a or b", "a, b or c"."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} or {parts[-1]}"


def no_results_hint(
    collections: list[Collection], start: str, end: str, max_cloud: float | None
) -> str:
    """What to try when a search found nothing, from what is known without asking."""
    if len(collections) == 1 and not overlaps_period(collections[0], start, end):
        first, last = collections[0].interval
        span = f"{(first or '')[:10]} to {(last or 'today')[:10]}".strip()
        return f"No scenes: data exists from {span}. Adjust the period."
    return f"No scenes found. You could {_or_join(_tips(collections, max_cloud))}."


def _tips(collections: list[Collection], max_cloud: float | None) -> list[str]:
    """Settings worth relaxing, in the order they most often help."""
    tips = []
    if any(c.time_filter for c in collections):
        tips.append("widen the period")
    cloud_limited = max_cloud is not None and max_cloud < 100
    if cloud_limited and any(c.has_cloud_cover for c in collections):
        tips.append(f"raise the maximum cloud cover ({max_cloud:g} %)")
    return [*tips, "choose another area"]


def search_many(
    collections: list[Collection],
    geometry_wgs84: dict,
    start: str,
    end: str,
    max_cloud: float | None,
    max_items_each: int,
    feedback: QgsFeedback | None = None,
    on_collection_done: Callable[[Collection, int, str | None], None] | None = None,
) -> SearchOutcome:
    """Search all collections in parallel; one failing catalogue does not stop the rest."""
    outcome = SearchOutcome()

    def work(c: Collection):
        if feedback is not None and feedback.isCanceled():
            return c, [], 0, None
        try:
            items, discarded = stac.search(
                PROVIDERS[c.provider],
                c,
                geometry_wgs84,
                start,
                end,
                max_cloud if c.has_cloud_cover else None,
                max_items_each,
                feedback,
            )
        except (RuntimeError, ValueError, OSError) as exc:  # network, JSON, geometry
            return c, [], 0, str(exc)
        return c, items, discarded, None

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(work, c) for c in collections]
        for future in as_completed(futures):
            c, items, discarded, error = future.result()
            outcome.items.extend(tag(it, c) for it in items)
            outcome.discarded += discarded
            outcome.counts[c.key] = len(items)
            if error:
                outcome.errors[c.key] = error
            if on_collection_done:
                on_collection_done(c, len(items), error)
    outcome.items.sort(key=stac.item_datetime, reverse=True)
    return outcome
