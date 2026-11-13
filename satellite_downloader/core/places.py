"""Turn what a user types into an area of interest.

Accepts place names (Nominatim/OpenStreetMap), decimal or DMS coordinates and
UTM coordinates. Nominatim answers are not always usable as an area: a desert
may come back as a single node ("Atacama": bbox of 0.0001°), an archipelago as
61 000 km² ("Spitzbergen"). ``area_for`` applies simple rules for both cases.

Nominatim usage policy: at most one request per second, an identifying
User-Agent, no request per keystroke. Results are © OpenStreetMap contributors.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

from osgeo import ogr, osr

from . import geometry, stac

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_ATTRIBUTION = "Place search: © OpenStreetMap contributors (Nominatim)"
POINT_EXTENT_KM = 1.0  # smaller results count as a point
MAX_BBOX_SIDE_KM = 50.0  # larger results are not taken as they are

_lock = threading.Lock()
_last_request = 0.0


@dataclass
class Place:
    name: str
    lon: float
    lat: float
    # south, north, west, east in degrees; None for typed coordinates
    bbox: tuple[float, float, float, float] | None = None
    kind: str = ""


@dataclass
class Area:
    wkt: str  # polygon in EPSG:4326
    kind: str  # "circle", "bbox"
    width_km: float
    height_km: float
    note: str = ""


# -- coordinates ----------------------------------------------------------------

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_DMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[°º]\s*(?:(\d+(?:[.,]\d+)?)\s*['′’]\s*)?"
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:[\"″”]|''))?\s*([NSEWO])",
    re.IGNORECASE,
)
_UNSIGNED = r"\d+(?:[.,]\d+)?"
_HEMISPHERE_PAIRS = (
    re.compile(
        rf"\s*([NSEOW])\s*({_UNSIGNED})\s*[,;\s]\s*([NSEOW])\s*({_UNSIGNED})\s*",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\s*({_UNSIGNED})\s*([NSEOW])\s*[,;\s]\s*({_UNSIGNED})\s*([NSEOW])\s*",
        re.IGNORECASE,
    ),
)
_COORDINATE_NAME = re.compile(rf"(?:(?:Point|Punkt)\s+)?{_NUM}\s*,\s*{_NUM}")
_UTM = re.compile(
    r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s+(\d{5,7}(?:[.,]\d+)?)\s*[,;\s]\s*"
    r"(\d{6,8}(?:[.,]\d+)?)\s*$",
    re.IGNORECASE,
)


def _f(text: str) -> float:
    return float(text.replace(",", "."))


def _hemisphere_pair(text: str) -> tuple[float, float] | None:
    """(lon, lat) for "N50.94 E6.95" or "50.94N 6.95E" in either order."""
    prefixed, suffixed = _HEMISPHERE_PAIRS
    if match := prefixed.fullmatch(text):
        tokens = [(match[1], match[2]), (match[3], match[4])]
    elif match := suffixed.fullmatch(text):
        tokens = [(match[2], match[1]), (match[4], match[3])]
    else:
        return None
    values = {}
    for letter, number in tokens:
        letter = letter.upper()
        value = -_f(number) if letter in "SW" else _f(number)
        values["lat" if letter in "NS" else "lon"] = value
    if set(values) != {"lat", "lon"}:
        return None
    return _checked(values["lon"], values["lat"])


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Return (lon, lat) for typed coordinates, None if the text is no coordinate.

    Decimal pairs are read as "lat, lon" (as copied from most map services)
    unless the first value cannot be a latitude.
    """
    text = text.strip()
    dms = _DMS.findall(text)
    if len(dms) == 2:
        return _dms_pair(dms)
    if hemispheres := _hemisphere_pair(text):
        return hemispheres
    if utm := _UTM.match(text):
        return _utm(*utm.groups())
    return _decimal_pair(text)


def _dms_pair(dms: list[tuple[str, str, str, str]]) -> tuple[float, float] | None:
    """(lon, lat) from two degree/minute/second matches, one N/S and one E/W."""
    values = {}
    for deg, minutes, seconds, hemi in dms:
        value = (
            _f(deg)
            + (_f(minutes) / 60 if minutes else 0)
            + (_f(seconds) / 3600 if seconds else 0)
        )
        hemi = hemi.upper()
        values["lat" if hemi in "NS" else "lon"] = -value if hemi in "SW" else value
    if set(values) != {"lat", "lon"}:
        return None
    return _checked(values["lon"], values["lat"])


def _utm(
    zone: str, band: str, easting: str, northing: str
) -> tuple[float, float] | None:
    south = band.upper() < "N"  # latitude bands C-M lie south of the equator
    ref = geometry.srs(f"EPSG:{(32700 if south else 32600) + int(zone)}")
    to_wgs = osr.CoordinateTransformation(ref, geometry.srs("EPSG:4326"))
    lon, lat, _ = to_wgs.TransformPoint(_f(easting), _f(northing))
    return _checked(lon, lat)


def _decimal_pair(text: str) -> tuple[float, float] | None:
    """Read as "lat, lon" unless the first value cannot be a latitude."""
    pair = re.fullmatch(rf"\s*({_NUM})\s*[,;\s]\s*({_NUM})\s*", text)
    if not pair:
        return None
    a, b = _f(pair.group(1)), _f(pair.group(2))
    if abs(a) > 90 >= abs(b):
        return _checked(a, b)
    return _checked(b, a)


def _checked(lon: float, lat: float) -> tuple[float, float] | None:
    if -180 <= lon <= 180 and -90 <= lat <= 90:
        return lon, lat
    return None


def short_name(name: str) -> str:
    """Name for labels: the part before the first comma, but all of a coordinate."""
    name = name.strip()
    if _COORDINATE_NAME.fullmatch(name):
        return name
    return name.partition(",")[0].strip()


# -- geocoding -------------------------------------------------------------------


def geocode(
    query: str, feedback=None, limit: int = 5, language: str = "en"
) -> list[Place]:
    """Places for a name, or a single place for typed coordinates."""
    coords = parse_coordinates(query)
    if coords:
        lon, lat = coords
        return [Place(f"{lat:.5f}, {lon:.5f}", lon, lat, None, "coordinate")]

    global _last_request
    with _lock:  # Nominatim: max. 1 request per second
        wait = 1.05 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    url = (
        f"{NOMINATIM_URL}?q={quote(query)}&format=jsonv2&limit={limit}"
        f"&accept-language={language}"
    )
    places = []
    for hit in json.loads(stac.request(url, None, feedback)):
        south, north, west, east = (float(v) for v in hit["boundingbox"])
        places.append(
            Place(
                hit.get("display_name", query),
                float(hit["lon"]),
                float(hit["lat"]),
                (south, north, west, east),
                f"{hit.get('category', '')}/{hit.get('type', '')}".strip("/"),
            )
        )
    return places


# -- area rules ------------------------------------------------------------------


def _extent_km(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    south, north, west, east = bbox
    mid_lat = math.radians((south + north) / 2)
    width = (east - west) % 360 * geometry.METRES_PER_DEGREE * math.cos(mid_lat) / 1000
    height = (north - south) * geometry.METRES_PER_DEGREE / 1000
    return width, height


def circle(lon: float, lat: float, radius_km: float, segments: int = 64) -> str:
    """Circle as WGS84 polygon WKT, built in a metric projection around the point."""
    point = ogr.CreateGeometryFromWkt(f"POINT ({lon} {lat})")
    local = geometry.srs(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    point.Transform(osr.CoordinateTransformation(geometry.srs("EPSG:4326"), local))
    disc = point.Buffer(radius_km * 1000, segments // 4)
    disc.Transform(osr.CoordinateTransformation(local, geometry.srs("EPSG:4326")))
    return disc.ExportToWkt()


def area_for(place: Place, radius_km: float = 5.0, whole_extent: bool = False) -> Area:
    """Area of interest for a place.

    - point or tiny bbox (< 1 km): circle with ``radius_km``
    - bbox up to 50 × 50 km, or any size with ``whole_extent``: the bbox
    - larger: circle around the centre, with a note explaining why
    """
    if place.bbox is not None:
        width, height = _extent_km(place.bbox)
        if max(width, height) >= POINT_EXTENT_KM:
            if whole_extent or max(width, height) <= MAX_BBOX_SIDE_KM:
                south, north, west, east = place.bbox
                wkt = (
                    f"POLYGON(({west} {south},{east} {south},{east} {north},"
                    f"{west} {north},{west} {south}))"
                )
                return Area(wkt, "bbox", width, height)
            side = 2 * radius_km
            return Area(
                circle(place.lon, place.lat, radius_km),
                "circle",
                side,
                side,
                note=(
                    f'"{short_name(place.name)}" is {width:.0f} × {height:.0f} km; '
                    f"using a circle with a {radius_km:g} km radius around the "
                    "centre. Increase the radius or draw a rectangle to cover more."
                ),
            )
    side = 2 * radius_km
    return Area(circle(place.lon, place.lat, radius_km), "circle", side, side)
