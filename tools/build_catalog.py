"""Build ``satellite_downloader/data/collections.json`` from the live catalogues.

Every collection of the registration-free providers is inspected: a sample
item is fetched, raster assets are selected, anonymous access is verified with
a range request, and the first asset's header is read for resolution, nodata
and data type. Collections that cannot be downloaded anonymously (requester
pays buckets, access-restricted data, vector/tabular/zarr products) are left
out and listed in the report.

Hand-tuned entries in ``core/catalog.py`` take precedence; generated entries
for the same provider and collection id are skipped.

Usage (QGIS Python, needs GDAL):
    C:\\OSGeo4W\\bin\\python-qgis.bat tools\\build_catalog.py
"""

from __future__ import annotations

import http.client
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osgeo import gdal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from satellite_downloader.core.catalog import CURATED, has_classification
from satellite_downloader.core.catalog import PROVIDERS as CATALOG_PROVIDERS
from satellite_downloader.core.version import PLUGIN_VERSION

gdal.UseExceptions()
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")

TARGET = ROOT / "satellite_downloader" / "data" / "collections.json"
REPORT = ROOT / "tools" / "catalog_report.md"
UA = {
    "User-Agent": f"QGIS-SatelliteDownloader-catalog/{PLUGIN_VERSION} (+https://github.com/d-handy/qgis-satellite-downloader)",
    "Accept": "application/json, application/geo+json",
}

# Providers are defined once in core/catalog.py; selection per provider is here.
PROVIDERS: dict[str, dict[str, Any]] = {
    key: {
        "url": p.stac_url,
        "method": p.search_method,
        "rewrites": p.href_rewrites,
        "signing": p.signing,
    }
    for key, p in CATALOG_PROVIDERS.items()
}
# swisstopo publishes hundreds of map layers; only imagery and elevation.
SWISSTOPO = {
    "ch.swisstopo.swissimage-dop10",
    "ch.swisstopo.swissalti3d",
    "ch.swisstopo.swisssurface3d-raster",
    "ch.swisstopo.swisseo_s2-sr_v200",
    "ch.swisstopo.swisseo_vhi_v100",
    "ch.swisstopo.swisseo_ndvi_z_v100",
    "ch.swisstopo.swisseo_ndvi_diff_v100",
    "ch.swisstopo.lubis-luftbilder_farbe",
    "ch.swisstopo.lubis-luftbilder_infrarot",
    "ch.swisstopo.lubis-luftbilder_schwarzweiss",
    "ch.bafu.landesforstinventar-vegetationshoehenmodell_sentinel",
}
# only: whitelist of collection ids; skip: regex of ids to leave out;
# label_prefix: for catalogues whose titles do not name the product.
PROVIDER_OPTIONS: dict[str, dict] = {
    "swisstopo": {"only": SWISSTOPO},
    # One collection per country; the title is just the country name.
    "worldpop": {"label_prefix": "WorldPop population – "},
    "kagis": {"label_prefix": "KAGIS – "},
    "lgln": {"label_prefix": "LGLN – "},
    # The Copernicus DEM is already offered by the Planetary Computer.
    "oam": {"skip": r"cop|dem"},
    # Embeddings are parquet tables.
    "earthgenome": {"skip": r"embedding"},
}
# Reachable, but not raster products for this plugin
SKIP_IDS = re.compile(
    r"(^arrivals$|tables|copc|census|gbif|fia$|ms-buildings|fws-nwi|eclipse)"
)

# Patterns are matched against whole words of "id title" (separators -> spaces).
CATEGORY_RULES = [
    ("Events", r"maxar|vantor|disaster|crisis response|emergency|building damage"),
    (
        "Radar",
        (
            r"sentinel 1|sentinel1|s1|sar|sar4tec\w*|palsar|jers|rtc|gamma0|gamma nought|"
            r"sigma0|ground motion|deformation|radarsat\w*|umbra|capella|iceye"
        ),
    ),
    ("Atmosphere", r"water vapou?r|gome 2|metop \w+|tropomi|sentinel 5p|s5p"),
    (
        "Population and settlement",
        (
            r"population|worldpop|built( area| up)?|settlements?|accessibility|wsf|"
            r"urbani[sz]ation|noise|human footprint|human modification|road density|"
            r"distance to roads|light pollution|vitality|labour \w+|mining density|"
            r"electricity access|human impact|anthropogenic biomes|nightlights|nighttime lights"
        ),
    ),
    (
        "Soil",
        r"soils?|soil \w+|oc iso|ocd|erosion|superficial deposits|peatland|sand|lithology|bulk density",
    ),
    (
        "Maps and boundaries",
        (
            r"topographic map|basic map|economic map|parish map|general map|contours|"
            r"municipalities|regions|country|country land|land parcels|building|"
            r"sea regions|regional state administrative agencies|protected areas"
        ),
    ),
    (
        "Elevation and terrain",
        (
            r"dem|dem \w+|alti3d|swissalti3d|surface3d\w*|swisssurface3d|topodata|3dep|"
            r"nasadem|srtm|elevation|bathymetry|bathy\w*|lidar|dsm|dtm|ndsm|"
            r"bare earth|surface model|surface height|canopy height|intensity|"
            r"swath separation|arcticdem|rema|topography|wetness index|gedtm\w*|edtm|geomorphons?|"
            r"hillshade|curvature|openness|shape index|slope \w+|ls factor|landform \w+"
        ),
    ),
    (
        "Aerial imagery",
        (
            r"naip|swissimage|dop|dop\d+|lubis|luftbilder|orthophoto\w*|orthofoto\w*|"
            r"orthoimagery|aerial|oam|openaerialmap|high resolution imagery|air photo\w*|"
            r"napl|drohnenfl\w*|drones?|topo dc austria op utm"
        ),
    ),
    (
        "Land cover and land use",
        (
            r"lulc|landcover|land cover|land use|worldcover|cci lc|cdl|lcmap|"
            r"c cap|gap|chesapeake|drcog|crop\w*|cereal\w*|worldcereal|nrcan|clum|mangrove\w*|gmw|"
            r"land class\w*|lccs|clc\w*|corine|arable land|lcv|landuse|imperviousness"
        ),
    ),
    ("Water", r"water|waterpack|wofs|gsw|floods?|lwq\w*|wq|dtw|wetlands?"),
    (
        "Vegetation and biomass",
        (
            r"ndvi|evi|vegetation|biomass|lai|fpar|gpp|npp|13q1|13a1|15a2h|15a3h|"
            r"17a2h|17a2hgf|17a3hgf|16a3gf|vhi|chloris|hgb|mobi|biodiversity|fc|evapotranspiration|"
            r"forests?|forest \w+|canopy cover|tree \w+|stand \w+|volume|grasslands?|mowing|"
            r"habitat\w*|fractional cover|carbon stock|site fertility class|site main class|"
            r"greenness|yield potential|veg|fapar|biome6k|biome00k|pasture|gross primary \w+"
        ),
    ),
    (
        "Temperature and climate",
        (
            r"lst|temperature|11a1|11a2|21a2|climate|daymet|era5|terraclimate|gridmet|"
            r"normals|nclimgrid|cmip\d*|samet|eta|chirps|rainfall|precipitation|mrms|imerg|met office|"
            r"wapor|iwmi|forecast|wind \w+|global radiation|humidity|pressure|aridity \w+|"
            r"chelsa|climatologies|bioclimatic|drought \w+"
        ),
    ),
    (
        "Atmosphere",
        r"sentinel 5p|s5p|aerosol|aod|goes\d*|cmi|glm|lightning|no2\w*|tropomi|ozone|o3",
    ),
    ("Ocean", r"ocean|sea surface|sst|ocsmart|rrs|sral|wst|altimetry"),
    (
        "Fire and snow",
        r"fire|14a1|14a2|64a1|mtbs|burn\w*|snow|10a1|10a2|frp|river ice|sea ice|wetsnow",
    ),
    (
        "Optical",
        (
            r"sentinel 2|s2|landsat|ls\d|hls\d*|aster|modis|mod\w+|myd\w+|cbers\w*|cb\d\w*|"
            r"amz\d|amazonia|resourcesat\w*|liss\d*|awifs|swisseo|sentinel 3|s3|olci|slstr|synergy|"
            r"gm|geomad|mosaics?|reflectance|optical|enmap|hsi|avhrr|spot\d*|skysat"
        ),
    ),
]

MASK_RULES: dict[str, dict] = {
    "SCL": {"kind": "classes", "invalid": [0, 1], "cloudy": [3, 8, 9, 10]},
    "QA_PIXEL": {"kind": "bits", "invalid": [0], "cloudy": [1, 2, 3, 4]},
    "CMASK": {"kind": "classes", "invalid": [0], "clear": [127]},
    "FMASK": {"kind": "bits", "cloudy": [1, 2, 3], "fill": 255},
}

# Digital Earth Africa publishes no readable collection metadata (see
# list_collections); readable names for its main products.
AFRICA_BBOX = [-26.0, -47.0, 64.0, 38.0]
DEAFRICA_TITLES = {
    "s2_l2a": "Sentinel-2 L2A (Africa)",
    "s2_l2a_c1": "Sentinel-2 L2A Collection 1 (Africa)",
    "ls5_sr": "Landsat 5 surface reflectance (Africa)",
    "ls7_sr": "Landsat 7 surface reflectance (Africa)",
    "ls8_sr": "Landsat 8 surface reflectance (Africa)",
    "ls9_sr": "Landsat 9 surface reflectance (Africa)",
    "ls5_st": "Landsat 5 surface temperature (Africa)",
    "ls7_st": "Landsat 7 surface temperature (Africa)",
    "ls8_st": "Landsat 8 surface temperature (Africa)",
    "ls9_st": "Landsat 9 surface temperature (Africa)",
    "landsat_c2l2_ar": "Landsat C2 L2 Analysis Ready (Africa)",
    "gm_s2_annual": "Sentinel-2 GeoMAD annual (cloud-free, Africa)",
    "gm_s2_annual_lowres": "Sentinel-2 GeoMAD annual, coarse (Africa)",
    "gm_s2_semiannual": "Sentinel-2 GeoMAD semiannual (Africa)",
    "gm_s2_rolling": "Sentinel-2 GeoMAD rolling (Africa)",
    "gm_ls5_ls7_annual": "Landsat 5/7 GeoMAD annual (Africa)",
    "gm_ls8_annual": "Landsat 8 GeoMAD annual (Africa)",
    "gm_ls8_ls9_annual": "Landsat 8/9 GeoMAD annual (Africa)",
    "s1_rtc": "Sentinel-1 RTC (Africa)",
    "s1_monthly_mosaic": "Sentinel-1 monthly mosaic (Africa)",
    "alos_palsar_mosaic": "ALOS/ALOS-2 PALSAR mosaic (Africa)",
    "jers_sar_mosaic": "JERS-1 SAR mosaic (Africa)",
    "wofs_ls": "Water observations WOfS, Landsat (Africa)",
    "wofs_ls_summary_annual": "Water frequency WOfS annual (Africa)",
    "wofs_ls_summary_alltime": "Water frequency WOfS all-time (Africa)",
    "fc_ls": "Fractional Cover, Landsat (Africa)",
    "fc_ls_summary_annual": "Fractional Cover annual (Africa)",
    "rainfall_chirps_daily": "Rainfall CHIRPS daily (Africa)",
    "rainfall_chirps_monthly": "Rainfall CHIRPS monthly (Africa)",
    "crop_mask": "Cropland mask (Africa)",
    "io_lulc_v2": "Land cover Impact Observatory (Africa)",
    "ndvi_anomaly": "NDVI anomaly (Africa)",
    "ndvi_climatology_ls": "NDVI climatology, Landsat (Africa)",
    "wapor_soil_moisture": "Soil moisture WaPOR (Africa)",
    "gmw": "Global Mangrove Watch (Africa)",
    "wq_annual": "Water quality annual (Africa)",
    "dem_cop_30": "Copernicus DEM 30 m (Africa)",
    "dem_cop_90": "Copernicus DEM 90 m (Africa)",
    "dem_srtm": "SRTM DEM (Africa)",
    "esa_worldcover_2020": "ESA WorldCover 2020 (Africa)",
    "esa_worldcover_2021": "ESA WorldCover 2021 (Africa)",
    "s3_olci_l2_lfr": "Sentinel-3 OLCI Land (Africa)",
    "s3_olci_l2_wfr": "Sentinel-3 OLCI Water (Africa)",
    "s3_slstr_l2_lst": "Sentinel-3 SLSTR land surface temperature (Africa)",
    "s3_syn_2_vg1": "Sentinel-3 Synergy VG1 (Africa)",
}

QUALITY_WORDS = re.compile(
    r"(qa|mask|flag|angle|azimuth|zenith|metadata|quality|provenance|"
    r"count|date|doy|time|uncert|stdev|std|obs|clouds?)",
    re.IGNORECASE,
)


def get(url: str, body: dict | None = None, timeout: int = 90):
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"not a web address: {url[:80]}")
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            # The scheme is checked at the top of the function.
            req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310
            with urllib.request.urlopen(req, timeout=timeout) as reply:  # noqa: S310 # nosec B310
                return json.load(reply)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (ConnectionError, TimeoutError, http.client.HTTPException):
            # e.g. "Remote end closed connection without response" under load
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(url)


def list_collections(key: str, base: str) -> list[dict]:
    out: list[dict] = []
    url: str | None = f"{base}/collections"
    pages = 0
    try:
        while url and pages < 60:
            page = get(url)
            out += page.get("collections", [])
            url = next(
                (
                    link["href"]
                    for link in page.get("links", [])
                    if link.get("rel") == "next"
                ),
                None,
            )
            pages += 1
    except (urllib.error.URLError, ValueError):
        out = []
    if not out:
        # Digital Earth Africa: /collections and the collection pages answer
        # "Server error" (HTML) while search works. Take the ids from the root
        # links and derive the metadata from a sample item later.
        root = get(base)
        out = []
        for link in root.get("links", []):
            if link.get("rel") != "child":
                continue
            cid = link["href"].rstrip("/").split("/")[-1]
            try:
                out.append(get(link["href"]))
            except (urllib.error.URLError, ValueError):
                out.append(
                    {
                        "id": cid,
                        "title": DEAFRICA_TITLES.get(
                            cid, f"Digital Earth Africa {cid}"
                        ),
                        "extent": {"spatial": {"bbox": [AFRICA_BBOX]}},
                    }
                )
    return out


def sample_items(provider: str, collection: dict, limit: int = 3) -> list[dict]:
    """A few items, so that asset keys that change between items are noticed."""
    cfg = PROVIDERS[provider]
    cid = collection["id"]
    if cfg["method"] == "GET":
        params = {"collections": cid, "limit": limit, "sortby": "-properties.datetime"}
        page = get(f"{cfg['url']}/search?{urllib.parse.urlencode(params)}")
    else:
        page = get(f"{cfg['url']}/search", {"collections": [cid], "limit": limit})
    return (page.get("features") or [])[:limit]


def numbered_keys(
    assets: dict[str, dict], others: list[dict]
) -> tuple[dict[str, dict], dict[str, str]]:
    """Stable keys for asset keys that contain numbers (sheet names, dates).

    "5016-56_COG" becomes "COG" with the pattern ``^\\d+-\\d+_COG$``. Keys whose
    pattern does not match an asset of every other sample item are dropped.
    """
    renamed: dict[str, dict] = {}
    patterns: dict[str, str] = {}
    for key, asset in assets.items():
        if not re.search(r"\d", key) or all(key in o.get("assets", {}) for o in others):
            renamed[key] = asset  # stable, or optional without a number (VV/HH)
            continue
        pattern = "^" + re.sub(r"\d+", r"\\d+", re.escape(key)) + "$"
        if sum(1 for k in assets if re.search(pattern, k)) > 1:
            continue  # e.g. soil depths "wv0033_0-5cm", "wv1500_60-100cm"
        if not all(
            any(re.search(pattern, k) for k in other.get("assets", {}))
            for other in others
        ):
            continue
        virtual = re.sub(r"[-_. ]{2,}", "_", re.sub(r"\d+", "", key)).strip("-_. ")
        virtual = virtual or "file"
        if virtual in renamed:
            continue
        renamed[virtual] = asset
        patterns[virtual] = pattern
    return renamed, patterns


_pc_tokens: dict[tuple[str, str], str] = {}


def resolve_href(provider: str, href: str) -> str:
    for pattern, replacement in PROVIDERS[provider]["rewrites"]:
        href = re.sub(pattern, replacement, href)
    m = re.match(r"https://([^.]+)\.blob\.core\.windows\.net/([^/]+)/", href)
    if PROVIDERS[provider]["signing"] == "planetary-computer" and m:
        key = (m.group(1), m.group(2))
        if key not in _pc_tokens:
            _pc_tokens[key] = get(
                f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{key[0]}/{key[1]}"
            )["token"]
        return f"{href}?{_pc_tokens[key]}"
    return href


def asset_format(asset: dict) -> str | None:
    kind = (asset.get("type") or "").lower()
    href = asset.get("href", "").lower().split("?")[0]
    if "tiff" in kind or href.endswith((".tif", ".tiff")):
        return "tif"
    if "netcdf" in kind or href.endswith(".nc"):
        return "netcdf"
    if "hdf" in kind or href.endswith((".hdf", ".h5", ".he5")):
        return "hdf"
    return None


def virtual_keys(collection_id: str, assets: dict) -> tuple[dict, dict]:
    """swisstopo asset keys are file names; derive stable keys plus regex patterns."""
    patterns: dict[str, str] = {}
    renamed: dict[str, dict] = {}
    for key, asset in assets.items():
        m = re.search(
            r"_(b\d{2}|b8a|tci|scl|aot|wvp|vhi|ndvi\w*|\w+)_(\d+m)\.tif$", key
        )
        if collection_id.startswith("ch.swisstopo.swisseo") and m:
            vkey = m.group(1)
            pattern = rf"_{re.escape(m.group(1))}_\d+m\.tif$"
        else:
            m = re.search(r"_(\d+(?:\.\d+)?)_\d{4}(?:_\d{4})?\.tif$", key)
            if not m:
                continue
            vkey = f"{m.group(1)} m"
            pattern = rf"_{re.escape(m.group(1))}_\d{{4}}(?:_\d{{4}})?\.tif$"
        patterns[vkey] = pattern
        renamed[vkey] = asset
    return renamed, patterns


def probe_header(url: str, item: dict) -> dict:
    ds = gdal.Open("/vsicurl/" + url)
    band = ds.GetRasterBand(1)
    info = {
        "nodata": band.GetNoDataValue(),
        "dtype": gdal.GetDataTypeName(band.DataType),
        "bands": ds.RasterCount,
    }
    ref = ds.GetSpatialRef()
    gt = ds.GetGeoTransform()
    if ds.GetGCPCount() and not ref:
        info["res"] = None
        return info
    size = abs(gt[1])
    if ref is not None and ref.IsGeographic():
        # Pixel size along the equator; outputs are metric, so this is the
        # finest resolution the source can deliver.
        size = size * 111_320
    elif ref is not None and ref.GetLinearUnits() not in (0, 1):
        size *= ref.GetLinearUnits()
    info["res"] = float(f"{size:.3g}") if size >= 1 else round(size, 3)
    return info


S2_BANDS = {
    "coastal": "B01",
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "nir": "B08",
    "nir08": "B8A",
    "swir16": "B11",
    "swir22": "B12",
}
OLI_BANDS = {
    "coastal": 1,
    "blue": 2,
    "green": 3,
    "red": 4,
    "nir08": 5,
    "swir16": 6,
    "swir22": 7,
}
TM_BANDS = {"blue": 1, "green": 2, "red": 3, "nir08": 4, "swir16": 5, "swir22": 7}
COMMON_NAMES = {
    "coastal",
    "blue",
    "green",
    "red",
    "nir",
    "nir08",
    "swir16",
    "swir22",
    "pan",
}


def infer_bands(cid: str, keys: list[str]) -> dict[str, str]:
    """Common band names for catalogues without ``eo:bands`` per asset.

    Covers asset keys that already are common names (INPE Landsat), the
    Sentinel-2 scheme (B02 ... B12) and Landsat SR_Bn/AR_Bn, whose numbering
    differs between OLI (Landsat 8/9) and TM/ETM+ (Landsat 4/5/7).
    """
    found: dict[str, str] = {}
    lower = {k.lower(): k for k in keys}
    for name in COMMON_NAMES:
        if name in lower:
            found[name] = lower[name]
    if {"B02", "B03", "B04"} <= set(keys):
        for name, key in S2_BANDS.items():
            if key in keys:
                found.setdefault(name, key)
    text = cid.lower()
    scheme = (
        TM_BANDS
        if any(t in text for t in ("ls5", "ls7", "ls4", "_tm", "etm"))
        else OLI_BANDS
    )
    for prefix in ("SR_B", "AR_B"):
        if any(k.startswith(prefix) for k in keys):
            for name, number in scheme.items():
                key = f"{prefix}{number}"
                if key in keys:
                    found.setdefault(name, key)
            if "nir08" in found:
                found.setdefault("nir", found["nir08"])
    return found


def category_for(cid: str, title: str) -> str:
    """First matching rule; patterns must match whole words of id and title."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", f"{cid} {title}".lower()) + " "
    for name, pattern in CATEGORY_RULES:
        if re.search(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", text):
            return name
    return "Other"


def region_hint(bbox: list | None) -> str:
    if not bbox or len(bbox) < 4 or bbox[0] is None:
        return ""
    w, s, e, n = bbox[:4]
    if e - w >= 300 and n - s >= 120:
        return "global"
    return f"{s:.0f}° to {n:.0f}° lat, {w:.0f}° to {e:.0f}° lon"


def build_entry(
    provider: str, collection: dict, curated: set, item: dict | None = None
) -> tuple[dict | None, str]:
    """Catalogue entry from a collection and a sample item (searched if not given)."""
    cid = collection["id"]
    if (provider, cid) in curated:
        return None, "hand-maintained"
    options = PROVIDER_OPTIONS.get(provider, {})
    if "only" in options and cid not in options["only"]:
        return None, "not selected"
    if "skip" in options and re.search(options["skip"], cid, re.IGNORECASE):
        return None, "not selected"
    if SKIP_IDS.search(cid):
        return None, "not a raster product"
    try:
        samples = [item] if item else sample_items(provider, collection)
    except (urllib.error.URLError, ValueError, RuntimeError) as exc:
        return None, f"search failed: {type(exc).__name__}"
    if not samples:
        return None, "no items"
    item = samples[0]

    raw_assets = {
        k: a
        for k, a in item.get("assets", {}).items()
        if asset_format(a)
        and "thumbnail" not in (a.get("roles") or [])
        and "overview" not in (a.get("roles") or [])
    }
    patterns: dict[str, str] = {}
    if provider == "swisstopo":
        raw_assets = {k: a for k, a in raw_assets.items() if asset_format(a) == "tif"}
        raw_assets, patterns = virtual_keys(cid, raw_assets)
    elif any(
        k not in other.get("assets", {}) and re.search(r"\d", k)
        for other in samples[1:]
        for k in raw_assets
    ):
        raw_assets, patterns = numbered_keys(raw_assets, samples[1:])
        if not raw_assets:
            return None, "asset keys change from item to item"
    tifs = {k: a for k, a in raw_assets.items() if asset_format(a) == "tif"}
    mode = "cog" if tifs else "raw"
    assets = tifs or raw_assets
    if not assets:
        return None, "no raster assets (e.g. Zarr, JP2, vector)"

    first_key = next(iter(assets))
    try:
        url = resolve_href(provider, assets[first_key]["href"])
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        return None, f"signing failed: {type(exc).__name__}"
    if url.startswith("s3://"):
        return None, "only via S3 (requester pays)"
    if not url.startswith(("https://", "http://")):
        return None, "not a web address"
    try:
        # The scheme is checked just above.
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-99"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=60) as reply:  # noqa: S310 # nosec B310
            if reply.status not in (200, 206):
                return None, f"access HTTP {reply.status}"
    except urllib.error.HTTPError as exc:
        return None, f"access HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, f"access: {type(exc).__name__}"

    props = item.get("properties", {})

    gsds = []
    commons: dict[str, str] = {}
    nodata = None
    for key, asset in assets.items():
        gsd = asset.get("gsd")
        for rb in asset.get("raster:bands") or []:
            gsd = gsd or rb.get("spatial_resolution")
            if nodata is None and rb.get("nodata") is not None and key == first_key:
                nodata = rb["nodata"]
        if gsd:
            gsds.append(float(gsd))
        for band in asset.get("eo:bands") or asset.get("bands") or []:
            if (
                band.get("common_name")
                and len(asset.get("eo:bands") or asset.get("bands") or []) == 1
            ):
                commons.setdefault(band["common_name"], key)
    for name, key in infer_bands(cid, list(assets)).items():
        commons.setdefault(name, key)

    nir = commons.get("nir") or commons.get("nir08")
    if all(c in commons for c in ("red", "green", "blue")):
        defaults = [commons["red"], commons["green"], commons["blue"]]
    elif (
        nir and "red" in commons and "green" in commons
    ):  # e.g. Landsat MSS: false colour
        defaults = [nir, commons["red"], commons["green"]]
    elif any(re.search(r"(^|_)(visual|tci|rgb)$", k, re.IGNORECASE) for k in assets):
        defaults = [
            next(
                k
                for k in assets
                if re.search(r"(^|_)(visual|tci|rgb)$", k, re.IGNORECASE)
            )
        ]
    elif patterns and all(re.fullmatch(r"[\d.]+ m", k) for k in assets):
        # swisstopo: one asset per resolution; offer the finest by default
        defaults = [min(assets, key=lambda k: float(k.split()[0]))]
    elif any(re.search(r"(^|[/_ ])cog($|[/_ ])", k, re.IGNORECASE) for k in assets):
        # the same raster offered as COG and as plain GeoTIFF or mosaic
        defaults = [
            next(
                k
                for k in assets
                if re.search(r"(^|[/_ ])cog($|[/_ ])", k, re.IGNORECASE)
            )
        ]
    else:
        useful = [k for k in assets if not QUALITY_WORDS.search(k)] or list(assets)
        defaults = useful[:3]

    def asset_gsd(key: str) -> float | None:
        m = re.fullmatch(r"([\d.]+) m", key)
        if m:
            return float(m.group(1))
        asset = assets[key]
        gsd = asset.get("gsd") or next(
            (
                b.get("spatial_resolution")
                for b in asset.get("raster:bands") or []
                if b.get("spatial_resolution")
            ),
            None,
        )
        return float(gsd) if gsd else None

    header: dict = {}
    if mode == "cog":
        try:
            header = probe_header(
                resolve_href(provider, assets[defaults[0]]["href"]), item
            )
        except (RuntimeError, urllib.error.URLError, KeyError) as exc:
            return None, f"GeoTIFF not readable: {str(exc)[:60]}"
    if nodata is None:
        nodata = header.get("nodata")
    if isinstance(nodata, str):
        nodata = None if nodata.lower() == "nan" else float(nodata)
    if isinstance(nodata, float) and math.isnan(nodata):  # GDAL handles NaN itself
        nodata = None

    default_gsds = [g for g in (asset_gsd(k) for k in defaults) if g]
    resolution = (
        (min(default_gsds) if default_gsds else None)
        or header.get("res")
        or (min(gsds) if gsds else None)
    )
    if resolution and header.get("res") and header["res"] > resolution * 100:
        # "gsd" given in degrees (GEO BON, EODC): trust the file
        resolution = header["res"]
    if mode == "cog" and not resolution:
        return None, "resolution undeterminable (e.g. GCPs only)"

    mask = None
    for key in assets:
        rule = MASK_RULES.get(key.upper())
        if rule:
            mask = {"asset": key, **rule}
            break

    # Class bands the asset key alone does not reveal (the plugin checks keys
    # itself, see catalog.CATEGORICAL_ASSET); search results may lack the
    # classification fields that the collection's item_assets carry.
    item_assets = collection.get("item_assets") or {}
    categorical = [
        k
        for k, a in assets.items()
        if has_classification(a) or has_classification(item_assets.get(k))
    ]

    group_by = []
    if "sat:orbit_state" in props and "sat:relative_orbit" in props:
        group_by = ["sat:orbit_state", "sat:relative_orbit"]
    elif "landsat:wrs_path" in props:
        group_by = ["landsat:wrs_path"]
    elif "path" in props:
        group_by = ["path"]

    extent = collection.get("extent", {})
    bbox = (extent.get("spatial", {}).get("bbox") or [None])[0]
    interval = (extent.get("temporal", {}).get("interval") or [[None, None]])[0]
    start, end = (interval + [None, None])[:2]
    static = bool(start and end and start[:10] == end[:10])

    title = (
        options.get("label_prefix", "")
        + re.sub(r"\s+", " ", collection.get("title") or cid).strip()
    )
    period = f"{(start or '?')[:4]}–{(end or 'today')[:4] if end else 'today'}"
    res_text = f"{resolution:g} m" if resolution else "resolution per file"
    parts = [f"{res_text}", period]
    hint = region_hint(bbox)
    if hint:
        parts.append(hint)
    if mode == "raw":
        parts.append(
            "whole files ("
            + ", ".join(sorted({asset_format(a) or "?" for a in assets.values()}))
            + ")"
        )
    note = "Added automatically · " + " · ".join(parts)

    entry = {
        "key": f"{provider}:{cid}",
        "label": title,
        "provider": provider,
        "collection_id": cid,
        "category": category_for(cid, title),
        "assets": list(assets),
        "default_assets": defaults,
        "native_res": resolution or 1000.0,
        "mode": mode,
        "nodata": nodata,
        "has_cloud_cover": "eo:cloud_cover" in props,
        "time_filter": not static,
        "note": note,
        "group_by": group_by,
        "mask": mask,
        "categorical_assets": categorical,
        "bands": commons,
        "asset_patterns": patterns,
        "bbox": bbox,
        "interval": [start, end],
        "license": collection.get("license"),
    }
    return entry, "ok"


def main() -> int:
    curated = {(c.provider, c.collection_id) for c in CURATED}
    entries: list[dict] = []
    skipped: list[tuple[str, str, str]] = []
    for provider, cfg in PROVIDERS.items():
        if cfg["method"] not in ("GET", "POST"):
            continue  # static catalogues: tools/build_static_index.py
        try:
            cols = list_collections(provider, cfg["url"])
        except (urllib.error.URLError, ValueError) as exc:
            print(f"{provider}: collections not readable: {exc}", file=sys.stderr)
            continue
        print(f"{provider}: {len(cols)} collections", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(6) as pool:
            results = list(
                pool.map(lambda c, p=provider: (c, build_entry(p, c, curated)), cols)
            )
        for col, (entry, reason) in results:
            if entry and any(e["key"] == entry["key"] for e in entries):
                continue  # listed twice by the catalogue (GEO BON)
            if entry:
                entries.append(entry)
            elif reason not in ("not selected",):
                skipped.append((provider, col["id"], reason))
    entries.sort(key=lambda e: (e["category"], e["label"].lower(), e["provider"]))
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    TARGET.write_text(
        json.dumps(
            {"built": stamp, "collections": entries}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    lines = [
        f"# Catalogue report ({stamp})",
        "",
        f"**{len(entries)} collections included**, {len(skipped)} left out.",
        "",
        "## Included",
        "",
        "| Category | Collection | Provider | Mode | Resolution |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {e['category']} | {e['label']} | {e['provider']} | {e['mode']} | "
        f"{e['native_res']:g} m |"
        for e in entries
    ]
    lines += [
        "",
        "## Left out",
        "",
        "| Provider | Collection | Reason |",
        "|---|---|---|",
    ]
    lines += [f"| {p} | {c} | {r} |" for p, c, r in sorted(skipped)]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"{len(entries)} included, {len(skipped)} left out -> {TARGET}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
