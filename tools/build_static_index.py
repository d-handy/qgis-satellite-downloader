"""Build search indexes for static STAC catalogues (no search API).

Each source is crawled from its start URLs (child and item links). Items are
grouped into plugin collections, one sample item per group runs through the
same checks as the API catalogue (``build_catalog.build_entry``: anonymous
access, header, bands, nodata), and the result is written per provider:

- ``satellite_downloader/data/static/<provider>.json``: collection entries
- ``satellite_downloader/data/static/<provider>.items.json.gz``: item rows,
  see ``satellite_downloader/core/static_index.py``

Usage (QGIS Python, needs GDAL):
    C:\\OSGeo4W\\bin\\python-qgis.bat tools\\build_static_index.py [provider ...]
"""

from __future__ import annotations

import gzip
import http.client
import json
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin

import build_catalog as bc

STATIC = bc.ROOT / "satellite_downloader" / "data" / "static"
REPORT = bc.ROOT / "tools" / "static_index_report.md"
MAX_BYTES = 3_000_000  # compressed item index per provider
PGC = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com"

# urls: where the crawl starts (defaults to the provider's catalogue URL)
# group: "collection" = nearest STAC Collection above the items, or the depth
#   of the node below the start URL (0 = the start node itself)
# prune: regex on node ids that are not followed
# static: products without a meaningful acquisition period (DEMs, hazard maps)
# rename: (regex, key) for asset keys that vary between items
SOURCES: dict[str, dict] = {
    "pgc": {
        "urls": [f"{PGC}/arcticdem/mosaics/v4.1.json", f"{PGC}/rema/mosaics/v2.0.json"],
        "group": "collection",
        "static": True,
        "category": "Elevation and terrain",
    },
    "openlandmap": {"group": 1, "label_prefix": "OpenLandMap – "},
    "ecodatacube": {"group": 1, "label_prefix": "EcoDataCube – "},
    "glofas": {
        "group": 1,
        "static": True,
        "category": "Water",
        "label_prefix": "GloFAS flood hazard – ",
    },
    "spotcanada": {"group": 1, "category": "Optical"},
    "maxar": {"group": 1, "category": "Events", "label_prefix": "Maxar – "},
    "capella": {
        "urls": [
            (
                "https://capella-open-data.s3.us-west-2.amazonaws.com/stac/"
                "capella-open-data-by-product-type/capella-open-data-geo/collection.json"
            )
        ],
        "group": 0,
        "category": "Radar",
        "label": "Capella SAR – geocoded (GEO)",  # the collection is titled "GEO"
        "rename": [(r"^(HH|HV|VH|VV)$", "backscatter")],
    },
    # titles read "Planet Crisis Response — <event>"
    "planetdisaster": {"group": 1, "category": "Events"},
}

SKIP_ROLES = {"thumbnail", "overview", "metadata", "visual-preview"}


def fix_bbox(bbox: list | None, geometry: dict | None) -> list[float] | None:
    if bbox and len(bbox) == 6:
        bbox = [bbox[0], bbox[1], bbox[3], bbox[4]]
    if not bbox and geometry:
        coords = re.findall(
            r"-?\d+(?:\.\d+)?(?:e-?\d+)?", json.dumps(geometry["coordinates"])
        )
        xs, ys = [float(v) for v in coords[0::2]], [float(v) for v in coords[1::2]]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
    if not bbox:
        return None
    w, s, e, n = (float(v) for v in bbox)
    if abs(s) > 90 or abs(n) > 90:  # latitude and longitude swapped (OpenLandMap)
        w, s, e, n = s, w, n, e
    return [round(w, 5), round(s, 5), round(e, 5), round(n, 5)]


def simple_ring(geometry: dict | None, max_points: int = 21) -> list | None:
    """Exterior ring of a small polygon that is not just its bounding box.

    Tilted tiles (ArcticDEM/REMA in polar stereographic) cover far less than
    their longitude/latitude box; the ring keeps coverage checks exact.
    """
    if not geometry:
        return None
    if geometry.get("type") == "MultiPolygon" and len(geometry["coordinates"]) == 1:
        ring = geometry["coordinates"][0][0]  # PGC wraps each tile like this
    elif geometry.get("type") == "Polygon":
        ring = geometry["coordinates"][0]
    else:
        return None
    if len(ring) > max_points:
        return None
    xs = {round(p[0], 5) for p in ring}
    ys = {round(p[1], 5) for p in ring}
    if len(xs) <= 2 and len(ys) <= 2:
        return None  # axis-parallel rectangle: the bbox says the same
    lons = [p[0] for p in ring]
    if max(lons) - min(lons) > 180:
        return None  # crosses the antimeridian: the bbox handles that
    return [[round(p[0], 5), round(p[1], 5)] for p in ring]


def iso(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace(" ", "T", 1)


class Crawl:
    def __init__(self, provider: str, cfg: dict):
        self.provider = provider
        self.cfg = cfg
        self.groups: dict[str, dict] = {}
        self.errors = 0
        self.requests = 0
        self.lock = threading.Lock()

    def run(self) -> None:
        start = self.cfg.get("urls") or [bc.PROVIDERS[self.provider]["url"]]
        frontier: list[tuple[str, tuple]] = [(u, ()) for u in start]
        with ThreadPoolExecutor(24) as pool:
            while frontier:
                results = list(pool.map(self.fetch, frontier))
                frontier = [nxt for batch in results for nxt in batch]
                print(
                    f"  {self.provider}: {self.requests} requests, "
                    f"{sum(len(g['rows']) for g in self.groups.values())} Items",
                    file=sys.stderr,
                    flush=True,
                )

    def fetch(self, task: tuple[str, tuple]) -> list[tuple[str, tuple]]:
        url, path = task
        self.requests += 1
        try:
            doc = bc.get(url, timeout=60)
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            self.errors += 1
            return []
        if doc.get("type") == "Feature":
            self.add_item(url, doc, path)
            return []
        node = {
            "id": str(doc.get("id")),
            "type": doc.get("type") or "Catalog",
            "title": doc.get("title"),
            "license": doc.get("license"),
        }
        prune = self.cfg.get("prune")
        if prune and path and re.search(prune, node["id"]):
            return []
        path = (*path, node)
        return [
            (urljoin(url, link["href"]), path)
            for link in doc.get("links", [])
            if isinstance(link, dict) and link.get("rel") in ("child", "item")
        ]

    def group_node(self, path: tuple) -> dict:
        how = self.cfg["group"]
        if how == "collection":
            return next(
                (n for n in reversed(path) if n["type"] == "Collection"), path[-1]
            )
        return path[min(how, len(path) - 1)]

    def add_item(self, url: str, item: dict, path: tuple) -> None:
        if not path:
            return
        props = item.get("properties", {})
        assets = {}
        for key, asset in (item.get("assets") or {}).items():
            if (
                SKIP_ROLES & set(asset.get("roles") or [])
                or bc.asset_format(asset) != "tif"
            ):
                continue
            for pattern, name in self.cfg.get("rename", []):
                key = re.sub(pattern, name, key)
            asset = {**asset, "href": urljoin(url, asset["href"])}
            assets[key] = asset
        bbox = fix_bbox(item.get("bbox"), item.get("geometry"))
        if not assets or not bbox:
            return
        start = iso(props.get("start_datetime") or props.get("datetime"))
        end = iso(props.get("end_datetime") or props.get("datetime"))
        node = self.group_node(path)
        row = [
            item.get("id"),
            bbox,
            start,
            end,
            {k: a["href"] for k, a in assets.items()},
        ]
        ring = simple_ring(item.get("geometry"))
        if ring:
            row.append(ring)
        with self.lock:
            group = self.groups.setdefault(
                node["id"],
                {"node": node, "rows": [], "sample": None, "sample_date": None},
            )
            group["rows"].append(row)
            if group["sample"] is None or (start or "") > (group["sample_date"] or ""):
                group["sample"] = {**item, "assets": assets}
                group["sample_date"] = start


def union_bbox(rows: list[list]) -> list[float]:
    if any(r[1][0] > r[1][2] for r in rows):
        west, east = -180.0, 180.0
    else:
        west, east = min(r[1][0] for r in rows), max(r[1][2] for r in rows)
    return [west, min(r[1][1] for r in rows), east, max(r[1][3] for r in rows)]


def build_provider(provider: str, cfg: dict) -> tuple[list[dict], dict, list[str]]:
    crawl = Crawl(provider, cfg)
    crawl.run()
    options = bc.PROVIDER_OPTIONS.setdefault(provider, {})
    if cfg.get("label_prefix"):
        options["label_prefix"] = cfg["label_prefix"]
    entries: list[dict] = []
    index: dict[str, list] = {}
    notes = [f"{crawl.requests} requests, {crawl.errors} errors"]
    for gid, group in sorted(crawl.groups.items()):
        rows = group["rows"]
        counts = Counter(k for r in rows for k in r[4])
        keys = [k for k, n in counts.items() if n == len(rows)] or [
            counts.most_common(1)[0][0]
        ]
        sample = group["sample"]
        sample = {
            **sample,
            "assets": {k: a for k, a in sample["assets"].items() if k in keys},
        }
        if not sample["assets"]:
            sample = next(
                {**sample, "assets": {k: {"href": r[4][k]} for k in keys}}
                for r in rows
                if all(k in r[4] for k in keys)
            )
        dates = [d for r in rows for d in (r[2], r[3]) if d]
        collection = {
            "id": gid,
            "title": group["node"]["title"] or gid,
            "license": group["node"]["license"],
            "extent": {
                "spatial": {"bbox": [union_bbox(rows)]},
                "temporal": {
                    "interval": [
                        [min(dates) if dates else None, max(dates) if dates else None]
                    ]
                },
            },
        }
        entry, reason = bc.build_entry(provider, collection, set(), item=sample)
        if entry is None:
            notes.append(f"{gid}: {reason}")
            continue
        if cfg.get("category"):
            entry["category"] = cfg["category"]
        if cfg.get("label"):
            entry["label"] = cfg["label"]
        if cfg.get("static"):
            entry["time_filter"] = False
        entry["note"] = entry["note"].replace(
            "Added automatically", f"Static catalogue, {len(rows)} files"
        )
        wanted = set(entry["assets"])
        index[gid] = [
            [r[0], r[1], r[2], r[3], {k: h for k, h in r[4].items() if k in wanted}]
            + r[5:]
            for r in rows
            if wanted <= set(r[4])
        ]
        entries.append(entry)
    return entries, index, notes


def main(argv: list[str]) -> int:
    chosen = argv or list(SOURCES)
    STATIC.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failed = False
    for provider in chosen:
        print(f"{provider}: Crawl", file=sys.stderr, flush=True)
        entries, index, notes = build_provider(provider, SOURCES[provider])
        payload = gzip.compress(json.dumps(index, separators=(",", ":")).encode())
        items = sum(len(v) for v in index.values())
        summary = (
            f"{len(entries)} collections, {items} items, {len(payload) / 1e6:.2f} MB"
        )
        print(f"{provider}: {summary}", file=sys.stderr)
        if len(payload) > MAX_BYTES:
            print(f"{provider}: index too large, not written", file=sys.stderr)
            failed = True
            continue
        entries.sort(key=lambda e: (e["category"], e["label"].lower()))
        meta = {
            "built": stamp,
            "summary": summary,
            "notes": notes,
            "collections": entries,
        }
        (STATIC / f"{provider}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (STATIC / f"{provider}.items.json.gz").write_bytes(payload)
    write_report()
    return 1 if failed else 0


def write_report() -> None:
    """Report over all written indexes, also those not rebuilt in this run."""
    lines = ["# Static catalogue indexes", ""]
    for path in sorted(STATIC.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        lines += [
            f"## {path.stem} (as of {meta['built']}): {meta.get('summary', '')}",
            "",
            *[f"- {n}" for n in meta.get("notes", [])],
            *[
                f"- **{e['label']}** ({e['category']}, {e['native_res']:g} m)"
                for e in meta["collections"]
            ],
            "",
        ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
