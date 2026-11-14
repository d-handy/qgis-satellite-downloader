"""Quick preview through the Planetary Computer data API.

The server crops and resamples one scene to at most ``MAX_SIZE`` pixels. Without
that limit a 100 × 100 km request timed out (HTTP 504); with it, it took 1.6 s.
The result is for display only and never stored as a product.
"""

from __future__ import annotations

from urllib.parse import urlencode

from osgeo import ogr

from . import stac
from .recipes import RecipePlan

DATA_API = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
MAX_SIZE = 1024


def url_for(recipe_plan: RecipePlan) -> str | None:
    acq = recipe_plan.acquisition
    if acq.collection.provider != "pc" or recipe_plan.recipe.style == "sar":
        return None
    xmin, xmax, ymin, ymax = ogr.CreateGeometryFromWkt(
        recipe_plan.aoi_wkt
    ).GetEnvelope()
    if xmax - xmin > 180:
        return None  # crosses the antimeridian; the API expects one bbox
    item = max(acq.items, key=lambda i: i.get(stac.COVERAGE_KEY) or 0)
    query = [
        ("collection", acq.collection.collection_id),
        ("item", item["id"]),
        ("max_size", MAX_SIZE),
    ] + [("assets", a) for a in recipe_plan.assets]
    return f"{DATA_API}/{xmin},{ymin},{xmax},{ymax}.tif?{urlencode(query)}"


def fetch(recipe_plan: RecipePlan, target: str, feedback=None) -> str | None:
    url = url_for(recipe_plan)
    if url is None:
        return None
    data = stac.request(url, None, feedback)
    with open(target, "wb") as fh:
        fh.write(data)
    return target
