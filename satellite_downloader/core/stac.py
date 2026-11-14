"""Minimal STAC API client and anonymous URL signing.

Uses QgsBlockingNetworkRequest so QGIS proxy and SSL settings apply, and so it
can run inside a worker thread. No third-party packages are needed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from . import geometry, static_index
from .catalog import SEARCH_STATIC, Collection, Provider

# URL of the anonymous token endpoint, not a secret.
PC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"  # noqa: S105
COVERAGE_KEY = "satellite_downloader:aoi_coverage"
CLEAR_KEY = "satellite_downloader:aoi_clear"
REPO_URL = "https://github.com/d-handy/qgis-satellite-downloader"
HELP_URL = REPO_URL + "#readme"
USER_AGENT = f"QGIS-SatelliteDownloader/0.4 (+{REPO_URL})".encode()

# Planetary Computer limits the size of geometries in a search body.
_MAX_VERTICES = 500
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (2, 5, 15)
_MAX_PAGES = 200  # per request, against catalogues that never stop paging


class StacError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _sleep(seconds: float, feedback: QgsFeedback | None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if feedback and feedback.isCanceled():
            return
        time.sleep(0.2)


def _request_once(url: str, body: dict | None, feedback: QgsFeedback | None):
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"User-Agent", USER_AGENT)
    # Some STAC servers (Digital Earth Africa) answer with HTML without it.
    req.setRawHeader(b"Accept", b"application/json, application/geo+json")
    blocking = QgsBlockingNetworkRequest()
    if body is None:
        err = blocking.get(req, forceRefresh=True, feedback=feedback)
    else:
        req.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json"
        )
        err = blocking.post(
            req, QByteArray(json.dumps(body).encode()), feedback=feedback
        )
    reply = blocking.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    return err, blocking, reply, (int(status) if status else None)


def web_url(url: str) -> str:
    """``url`` if it is an http(s) address; anything else is refused.

    Catalogue responses decide which addresses are fetched (asset hrefs, next
    links). Without this check a catalogue could point GDAL or Qt at local
    files (``file:``, ``/vsi…`` paths) or other protocols.
    """
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise StacError(f"Refusing a non-web address: {url[:80]}")
    return url


def request(
    url: str, body: dict | None = None, feedback: QgsFeedback | None = None
) -> bytes:
    """GET (or POST with a JSON body); retries rate limits and server errors."""
    web_url(url)
    for attempt in range(len(_RETRY_DELAYS) + 1):
        err, blocking, reply, status = _request_once(url, body, feedback)
        if err == QgsBlockingNetworkRequest.ErrorCode.NoError:
            return bytes(reply.content())
        if feedback and feedback.isCanceled():
            raise StacError("Cancelled")
        retryable = status in _RETRY_STATUS or (
            status is None and err == QgsBlockingNetworkRequest.ErrorCode.NetworkError
        )
        if not retryable or attempt == len(_RETRY_DELAYS):
            detail = bytes(reply.content()).decode(errors="replace")[:300]
            raise StacError(
                f"{url}: {blocking.errorMessage()} {detail}".strip(), status
            )
        delay = _RETRY_DELAYS[attempt]
        retry_after = bytes(reply.rawHeader(b"Retry-After")).decode().strip()
        if retry_after.isdigit():
            delay = min(int(retry_after), 60)
        _sleep(delay, feedback)
    raise StacError(url)  # unreachable


def get_json(
    url: str, body: dict | None = None, feedback: QgsFeedback | None = None
) -> dict:
    return json.loads(request(url, body, feedback))


def search_geometry(aoi_geojson: dict) -> tuple[dict, geometry.ogr.Geometry]:
    """(GeoJSON for the API, OGR geometry for coverage checks), both in [-180, 180]."""
    folded = geometry.split_antimeridian(
        geometry.from_geojson(geometry.unwrap_antimeridian(aoi_geojson))
    )
    return geometry.to_geojson(
        geometry.simplify_covering(folded, _MAX_VERTICES)
    ), folded


def search(
    provider: Provider,
    collection: Collection,
    geometry_wgs84: dict,
    start: str,
    end: str,
    max_cloud: float | None,
    max_items: int,
    feedback: QgsFeedback | None = None,
) -> tuple[list[dict], int]:
    """Return (items newest first, number of discarded items).

    Some catalogues (INPE Brazil Data Cube) match ``intersects`` against the
    item bounding box, not its footprint. Tilted swaths such as CBERS-4A then
    return scenes that do not touch the AOI at all, which would produce files
    with nothing but nodata. Such items are discarded here; the rest carry
    their AOI coverage in ``COVERAGE_KEY``.
    """
    query_geom, aoi = search_geometry(geometry_wgs84)
    cloud_filter = (
        collection.has_cloud_cover and max_cloud is not None and max_cloud < 100
    )
    found = _Collector(
        provider, collection, aoi, max_items, max_cloud if cloud_filter else None
    )
    if provider.search_method == SEARCH_STATIC:
        envelopes = [part.GetEnvelope() for part in _polygon_parts(aoi)]
        found.accept(static_index.search(collection, envelopes, start, end))
    else:
        window = _SearchWindow(
            collection, min(max_items, 100), f"{start}T00:00:00Z/{end}T23:59:59Z"
        )
        if provider.search_method == "GET":
            requests = _get_requests(provider, window, aoi)
        else:
            requests = [_post_request(provider, window, query_geom, found.max_cloud)]
        for url, body in requests:
            _follow_pages(url, body, found, feedback)
    found.items.sort(key=item_datetime, reverse=True)
    return found.items[:max_items], found.discarded


@dataclass(frozen=True)
class _SearchWindow:
    collection: Collection
    limit: int  # page size
    datetime_range: str


def _get_requests(
    provider: Provider, window: _SearchWindow, aoi: geometry.ogr.Geometry
) -> list[tuple[str, dict | None]]:
    """GET APIs take a bbox; one per part when the AOI crosses the antimeridian."""
    requests: list[tuple[str, dict | None]] = []
    for part in _polygon_parts(aoi):
        xmin, xmax, ymin, ymax = part.GetEnvelope()
        params = {
            "collections": window.collection.collection_id,
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "limit": window.limit,
        }
        if window.collection.time_filter:
            params["datetime"] = window.datetime_range
        if provider.supports_sort:
            params["sortby"] = "-properties.datetime"
        requests.append((f"{provider.stac_url}/search?{urlencode(params)}", None))
    return requests


def _post_request(
    provider: Provider, window: _SearchWindow, query_geom: dict, max_cloud: float | None
) -> tuple[str, dict | None]:
    query: dict = {
        "collections": [window.collection.collection_id],
        "intersects": query_geom,
        "limit": window.limit,
    }
    if window.collection.time_filter:
        query["datetime"] = window.datetime_range
    if max_cloud is not None and provider.supports_query:
        query["query"] = {"eo:cloud_cover": {"lte": max_cloud}}
    if provider.supports_sort:
        # INPE rejects the short form "datetime"; all accept the full path.
        query["sortby"] = [{"field": "properties.datetime", "direction": "desc"}]
    return f"{provider.stac_url}/search", query


def _follow_pages(
    url: str | None, body: dict | None, found: _Collector, feedback
) -> None:
    pages = 0
    while url and not found.full() and pages <= _MAX_PAGES:
        if feedback and feedback.isCanceled():
            return
        page = get_json(url, body, feedback)
        found.accept(page.get("features", []))
        pages += 1
        url, body = next_page(page, body)


class _Collector:
    """Items accepted from search pages: unique, within the limit, touching the AOI.

    Cloud cover is filtered here as well, for providers without server-side
    "query" support (``max_cloud`` is None when no filter applies).
    """

    def __init__(self, provider, collection, aoi, max_items: int, max_cloud):
        self.provider, self.collection, self.aoi = provider, collection, aoi
        self.max_items, self.max_cloud = max_items, max_cloud
        self.items: list[dict] = []
        self.seen_ids: set[str] = set()
        self.discarded = 0

    def full(self) -> bool:
        return len(self.items) >= self.max_items

    def accept(self, features: list[dict]) -> None:
        for item in features:
            if self.full():
                return
            if str(item.get("id")) in self.seen_ids or self._too_cloudy(item):
                continue
            coverage = geometry.aoi_coverage(item.get("geometry"), self.aoi)
            if coverage is not None and coverage <= 0:
                self.discarded += 1
                continue
            item[COVERAGE_KEY] = coverage
            self.seen_ids.add(str(item.get("id")))
            self.items.append(normalize_item(item, self.provider, self.collection))

    def _too_cloudy(self, item: dict) -> bool:
        cloud = item.get("properties", {}).get("eo:cloud_cover")
        return (
            self.max_cloud is not None and cloud is not None and cloud > self.max_cloud
        )


def _polygon_parts(geom: geometry.ogr.Geometry) -> list[geometry.ogr.Geometry]:
    if geom.GetGeometryCount() > 1 and "MULTI" in geom.GetGeometryName():
        parts = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
        return [p for p in parts if p is not None]
    return [geom]


def normalize_item(item: dict, provider: Provider, collection: Collection) -> dict:
    """Make assets usable as the rest of the plugin expects them.

    - rewrite hrefs of public S3 buckets to HTTPS (Digital Earth Africa)
    - add stable virtual asset keys where the catalogue uses file names as
      keys (swisstopo: "…_0.1_2056.tif" becomes "0.1 m")
    """
    assets = item.get("assets", {})
    if provider.href_rewrites:
        for asset in assets.values():
            href = asset.get("href", "")
            for pattern, replacement in provider.href_rewrites:
                href = re.sub(pattern, replacement, href)
            asset["href"] = href
    for virtual, pattern in collection.asset_patterns.items():
        if virtual not in assets:
            match = next((a for k, a in assets.items() if re.search(pattern, k)), None)
            if match is not None:
                assets[virtual] = match
    return item


def item_datetime(item: dict) -> str:
    props = item["properties"]
    return props.get("datetime") or props.get("start_datetime") or ""


def next_page(page: dict, body: dict | None) -> tuple[str | None, dict | None]:
    for link in page.get("links", []):
        if link.get("rel") != "next":
            continue
        if link.get("method", "GET").upper() == "POST":
            nxt = link.get("body", {})
            return link["href"], ({**(body or {}), **nxt} if link.get("merge") else nxt)
        # GET links already encode the full query in the URL.
        return link["href"], None
    return None, None


class PlanetaryComputerSigner:
    """Appends anonymous SAS tokens; tokens are cached per storage container.

    Tokens must be requested per account/container, not per collection: some
    collections (e.g. the Copernicus DEM) live in a container whose collection
    token is rejected with HTTP 403. Tokens live about 45 minutes.
    """

    def __init__(self):
        self._tokens: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def sign(self, href: str, feedback: QgsFeedback | None = None) -> str:
        parsed = urlparse(web_url(href))
        if not parsed.netloc.endswith(".blob.core.windows.net"):
            return href
        account = parsed.netloc.split(".")[0]
        container = parsed.path.lstrip("/").split("/")[0]
        separator = "&" if parsed.query else "?"
        return f"{href}{separator}{self._token(account, container, feedback)}"

    def invalidate(self) -> None:
        """Drop cached tokens, e.g. after a 403 during a long read."""
        with self._lock:
            self._tokens.clear()

    def _token(self, account: str, container: str, feedback) -> str:
        key = (account, container)
        now = datetime.now(timezone.utc)
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - now > timedelta(minutes=15):
                return cached[0]
        data = get_json(f"{PC_TOKEN_URL}/{account}/{container}", feedback=feedback)
        expiry = datetime.fromisoformat(data["msft:expiry"].replace("Z", "+00:00"))
        with self._lock:
            self._tokens[key] = (data["token"], expiry)
        return data["token"]


class NullSigner:
    def sign(self, href: str, feedback: QgsFeedback | None = None) -> str:
        return web_url(href)

    def invalidate(self) -> None:
        pass


def signer_for(provider: Provider):
    if provider.signing == "planetary-computer":
        return PlanetaryComputerSigner()
    return NullSigner()
