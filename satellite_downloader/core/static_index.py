"""Search in the bundled indexes of static STAC catalogues.

Static catalogues (a tree of JSON files, no search API) are crawled once by
``tools/build_static_index.py``. Per provider it writes the collection metadata
(``static/<provider>.json``, loaded by ``catalog.load_generated``) and a
compact item list (``static/<provider>.items.json.gz``)::

    {collection_id: [[item_id, [w, s, e, n], start, end, {asset: href}, ring?], ...]}

``w > e`` marks an item crossing the antimeridian. The optional ``ring`` is the
exterior ring of simple footprints (tilted polar tiles); otherwise the bbox
serves as footprint. Items become STAC-like dicts, so coverage, footprints and
the download pipeline treat them like search results.
"""

from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path

from .catalog import GENERATED_FILE, STATIC_DIR, Collection

INDEX_DIR = GENERATED_FILE.parent / STATIC_DIR

_cache: dict[str, dict[str, list]] = {}
_lock = threading.Lock()


def items_file(provider: str, directory: Path = INDEX_DIR) -> Path:
    return directory / f"{provider}.items.json.gz"


def load(provider: str, directory: Path = INDEX_DIR) -> dict[str, list]:
    """Item index of a provider; cached, empty if the file is missing."""
    key = str(items_file(provider, directory))
    with _lock:
        if key not in _cache:
            path = items_file(provider, directory)
            if path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    _cache[key] = json.load(f)
            else:
                _cache[key] = {}
        return _cache[key]


def _boxes(bbox: list[float]) -> list[tuple[float, float, float, float]]:
    w, s, e, n = bbox
    if w > e:  # crosses the antimeridian
        return [(w, s, 180.0, n), (-180.0, s, e, n)]
    return [(w, s, e, n)]


def _intersects(bbox: list[float], envelopes: list[tuple]) -> bool:
    return any(
        w <= xmax and e >= xmin and s <= ymax and n >= ymin
        for w, s, e, n in _boxes(bbox)
        for xmin, xmax, ymin, ymax in envelopes
    )


def _footprint(bbox: list[float]) -> dict:
    rings = [[[[w, s], [e, s], [e, n], [w, n], [w, s]]] for w, s, e, n in _boxes(bbox)]
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": rings[0]}
    return {"type": "MultiPolygon", "coordinates": rings}


def to_item(row: list, collection: Collection) -> dict:
    item_id, bbox, start, end, assets = row[:5]
    props: dict = {"datetime": start}
    if end and end != start:
        props.update(start_datetime=start, end_datetime=end)
    if not collection.time_filter:
        # Tiles of a static product (DEM mosaic) carry their own source dates;
        # one date for all keeps them in one mosaic.
        props["datetime"] = collection.interval[1] or collection.interval[0] or start
    footprint = (
        {"type": "Polygon", "coordinates": [row[5]]}
        if len(row) > 5
        else _footprint(bbox)
    )
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "collection": collection.collection_id,
        "bbox": bbox,
        "geometry": footprint,
        "properties": props,
        "assets": {key: {"href": href} for key, href in assets.items()},
        "links": [],
    }


def search(
    collection: Collection,
    envelopes: list[tuple[float, float, float, float]],
    start: str,
    end: str,
    directory: Path = INDEX_DIR,
) -> list[dict]:
    """Items whose bbox touches one of the AOI envelopes, newest first.

    ``envelopes`` are OGR envelopes (xmin, xmax, ymin, ymax) of the AOI parts
    in WGS 84. The period is ignored for static products.
    """
    rows = load(collection.provider, directory).get(collection.collection_id, [])
    lo, hi = f"{start}T00:00:00Z", f"{end}T23:59:59Z"
    found = []
    for row in rows:
        if not _intersects(row[1], envelopes):
            continue
        if collection.time_filter and row[2]:
            first, last = row[2], row[3] or row[2]
            if first > hi or last < lo:
                continue
        found.append(to_item(row, collection))
    found.sort(key=lambda i: i["properties"]["datetime"] or "", reverse=True)
    return found
