"""Geometry helpers that must work for any place on Earth.

All functions take and return OGR geometries or GeoJSON dicts in EPSG:4326
with lon/lat axis order, unless stated otherwise.
"""

from __future__ import annotations

import json
import math

from osgeo import ogr, osr

METRES_PER_DEGREE = 111_320.0

# Beyond these latitudes UTM is undefined or strongly distorted.
_UTM_NORTH_LIMIT = 84.0
_UTM_SOUTH_LIMIT = -80.0
# AOIs wider than this (degrees of longitude) get an equal-area projection
# instead of a single UTM zone.
_MAX_UTM_WIDTH = 12.0


def srs(definition: str) -> osr.SpatialReference:
    """SpatialReference from EPSG code, WKT or PROJ string, lon/lat axis order."""
    ref = osr.SpatialReference()
    ref.SetFromUserInput(definition)
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ref


def auth_code(ref: osr.SpatialReference) -> str | None:
    if ref.GetAuthorityName(None) and ref.GetAuthorityCode(None):
        return f"{ref.GetAuthorityName(None)}:{ref.GetAuthorityCode(None)}"
    return None


def from_geojson(geojson: dict) -> ogr.Geometry:
    return ogr.CreateGeometryFromJson(json.dumps(geojson))


def to_geojson(geom: ogr.Geometry) -> dict:
    return json.loads(geom.ExportToJson())


# -- antimeridian -------------------------------------------------------------


def _map_coords(coords, fn):
    if isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_map_coords(c, fn) for c in coords]


def _iter_lons(coords):
    if isinstance(coords[0], (int, float)):
        yield coords[0]
    else:
        for c in coords:
            yield from _iter_lons(c)


def unwrap_antimeridian(geojson: dict) -> dict:
    """Make an AOI that crosses ±180° continuous by shifting it past 180°.

    A rectangle drawn around Fiji in a projected CRS arrives in EPSG:4326 as
    longitudes near +179 and -179; taken literally it spans the whole globe.
    If the literal extent exceeds 180° but the shifted one does not, negative
    longitudes are moved by +360°.
    """
    lons = list(_iter_lons(geojson["coordinates"]))
    if max(lons) - min(lons) <= 180:
        return geojson
    shifted = [lon + 360 if lon < 0 else lon for lon in lons]
    if max(shifted) - min(shifted) >= 180:
        return geojson  # genuinely global, nothing to unwrap
    return {
        "type": geojson["type"],
        "coordinates": _map_coords(
            geojson["coordinates"], lambda p: [p[0] + 360 if p[0] < 0 else p[0], *p[1:]]
        ),
    }


def _box(xmin, ymin, xmax, ymax) -> ogr.Geometry:
    return ogr.CreateGeometryFromWkt(
        f"POLYGON(({xmin} {ymin},{xmax} {ymin},{xmax} {ymax},{xmin} {ymax},{xmin} {ymin}))"
    )


def split_antimeridian(geom: ogr.Geometry) -> ogr.Geometry:
    """Fold an unwrapped geometry (longitudes > 180°) back into [-180, 180].

    STAC APIs and item footprints live in [-180, 180], so the AOI used for
    searching and for coverage checks must be split into a MultiPolygon.
    """
    xmin, xmax, _, _ = geom.GetEnvelope()
    if xmax <= 180 and xmin >= -180:
        return geom
    result = ogr.Geometry(ogr.wkbMultiPolygon)
    for window, shift in (
        (_box(-540, -90, -180, 90), 360),
        (_box(-180, -90, 180, 90), 0),
        (_box(180, -90, 540, 90), -360),
    ):
        part = geom.Intersection(window)
        if part is None or part.IsEmpty():
            continue
        for poly in _polygons(part):
            if shift:
                gj = to_geojson(poly)
                poly = from_geojson(
                    {
                        "type": "Polygon",
                        "coordinates": _map_coords(
                            gj["coordinates"], lambda p, s=shift: [p[0] + s, *p[1:]]
                        ),
                    }
                )
            result.AddGeometry(poly)
    return result


def _polygons(geom: ogr.Geometry) -> list[ogr.Geometry]:
    kind = ogr.GT_Flatten(geom.GetGeometryType())
    if kind == ogr.wkbPolygon:
        return [geom.Clone()]
    if kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        out: list[ogr.Geometry] = []
        for i in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(i)
            if part is not None:
                out.extend(_polygons(part))
        return out
    return []


# -- search geometry ------------------------------------------------------------


def vertex_count(geom: ogr.Geometry) -> int:
    return len(list(_iter_lons(to_geojson(geom)["coordinates"])))


def simplify_covering(geom: ogr.Geometry, max_vertices: int = 500) -> ogr.Geometry:
    """Simplify so the result still contains the input.

    Catalogues limit the size of search geometries. Buffering by the tolerance
    before simplifying keeps every original point inside, so no scene that
    touches the real AOI is lost (unlike shrinking to a simplified outline).
    """
    if vertex_count(geom) <= max_vertices:
        return geom
    xmin, xmax, ymin, ymax = geom.GetEnvelope()
    tol = max(xmax - xmin, ymax - ymin) / 1000
    for _ in range(20):
        simple = geom.Buffer(tol, 2).SimplifyPreserveTopology(tol)
        if vertex_count(simple) <= max_vertices:
            return simple
        tol *= 2
    return _box(xmin, ymin, xmax, ymax)


def aoi_coverage(footprint_geojson: dict | None, aoi: ogr.Geometry) -> float | None:
    """Share of the AOI (in %) inside a scene footprint, None if unknown.

    Computed in geographic degrees, which is accurate enough for a ratio over
    areas of a few hundred kilometres.
    """
    if not footprint_geojson:
        return None
    footprint = from_geojson(footprint_geojson)
    if footprint is None:
        return None
    if not footprint.IsValid():
        footprint = footprint.MakeValid()
    area = aoi.GetArea() if aoi.GetDimension() == 2 else 0.0
    if area == 0:
        return 100.0 if aoi.Intersects(footprint) else 0.0
    overlap = aoi.Intersection(footprint)
    if overlap is None:
        return None
    return min(100.0, 100.0 * overlap.GetArea() / area)


# -- output CRS -----------------------------------------------------------------


def auto_crs(aoi_unwrapped: ogr.Geometry) -> str:
    """CRS for an AOI when the scenes offer none: UTM, polar or equal-area.

    Returns an EPSG code or a PROJ string (for the equal-area case).
    """
    centroid = aoi_unwrapped.Centroid()
    lon = ((centroid.GetX() + 180) % 360) - 180
    lat = centroid.GetY()
    xmin, xmax, _, _ = aoi_unwrapped.GetEnvelope()
    if lat > _UTM_NORTH_LIMIT:
        return "EPSG:3995"  # WGS 84 / Arctic Polar Stereographic
    if lat < _UTM_SOUTH_LIMIT:
        return "EPSG:3031"  # WGS 84 / Antarctic Polar Stereographic
    if xmax - xmin > _MAX_UTM_WIDTH:
        return (
            f"+proj=laea +lat_0={lat:.4f} +lon_0={lon:.4f} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def resolution_in_crs(resolution_m: float, ref: osr.SpatialReference) -> float:
    """Pixel size in CRS units; geographic grids get square pixels in degrees.

    Square degrees are what geographic rasters conventionally use, but a pixel
    is then narrower east–west than north–south (see ``east_west_metres``).
    """
    return resolution_m / METRES_PER_DEGREE if ref.IsGeographic() else resolution_m


def east_west_metres(resolution_m: float, lat: float) -> float:
    """East–west ground size of a square-degree pixel at latitude ``lat``."""
    return resolution_m * math.cos(math.radians(lat))


def snap_bounds(bounds, resolution: float, origin: tuple[float, float]):
    """Extend bounds outward onto a pixel grid anchored at ``origin``.

    Landsat grids start at multiples of 30 m + 15 m; snapping to plain
    multiples of the resolution would shift every pixel by half a pixel.
    """
    ox = origin[0] % resolution
    oy = origin[1] % resolution
    xmin, ymin, xmax, ymax = bounds
    return (
        math.floor(round((xmin - ox) / resolution, 6)) * resolution + ox,
        math.floor(round((ymin - oy) / resolution, 6)) * resolution + oy,
        math.ceil(round((xmax - ox) / resolution, 6)) * resolution + ox,
        math.ceil(round((ymax - oy) / resolution, 6)) * resolution + oy,
    )


def project(
    geom_wgs84: ogr.Geometry, ref: osr.SpatialReference, segment_deg: float = 0.01
) -> ogr.Geometry:
    """Densify and project a (possibly unwrapped) WGS84 geometry."""
    geom = geom_wgs84.Clone()
    geom.Segmentize(segment_deg)
    geom.Transform(osr.CoordinateTransformation(srs("EPSG:4326"), ref))
    return geom
