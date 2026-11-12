# qgis-satellite-downloader

A QGIS plugin that loads satellite data for any place on Earth. You don't need an account,
an API key or any extra Python packages: it runs on QGIS and the GDAL that ships with it.

## Simple mode: place → purpose → load

Open *Raster → Satellite Downloader → Satellite data …*, or type `sat Atacama` into the QGIS
locator bar.

1. **Place:** enter a place name (looked up with OpenStreetMap/Nominatim) or coordinates such
   as `50.94, 6.95`, `N50.94 E6.95`, `50°56'N 6°57'E` or the UTM form `32U 356000 5645000`.
   You can also click on the map or use the current map extent.
   - Ambiguous names show up to five matches.
   - If the search returns only a point (e.g. "Atacama"), the plugin uses a circle with an
     adjustable radius.
   - Very large areas (e.g. "Svalbard") are limited to a circle around their centre, with
     a notice. On request, the plugin loads the whole area at a coarser resolution.
2. **Purpose:** choose one of three recipes.

   | Recipe | Data | Result |
   |---|---|---|
   | Current image | Sentinel-2 (10 m) and Landsat 8/9 (30 m), searched together | most recent acquisition with ≥ 95 % clear sky **within the area**, true colour |
   | Radar (through clouds, day and night) | Sentinel-1 RTC (10 m) | backscatter in dB; VV/VH, or HH/HV in polar regions |
   | Elevation and terrain | Copernicus DEM (30 m) | elevation, hillshade, slope |

3. **Load:** click *Load*. The plugin picks the scene, bands, resolution and coordinate
   reference system on its own. It shows a preview within seconds and then adds the result as
   a styled layer group "place – recipe – date", with the temporal controller set. The info
   panel lists sensor, date, clear-sky share and coverage, along with a source and licence
   notice you can copy.

If nothing suitable exists, the plugin explains why and suggests an alternative. Typical
reasons are polar night (with the computed date it ends), open sea or persistent cloud.

Results are saved in the project folder under `SatelliteData/`. Without a saved project they
go to `~/SatelliteDownloads/`, or to a folder chosen with *Change folder …*.

## Expert mode

*Raster → Satellite Downloader → Expert mode …* gives free access to **1267 collections from
27 providers** (as of 17 Sep 2026). You choose bands, date range, resolution, CRS and resampling
yourself.

- **Choosing datasets:** the list is searchable, including by band and asset names (e.g.
  "NDVI"). It is grouped by theme: optical, radar, elevation, land cover, water, vegetation,
  temperature/climate, atmosphere, ocean, fire/snow, aerial imagery, population/settlement,
  soil, maps/boundaries and events. The filter *Only for my area* hides
  datasets that do not cover your area. If a dataset ended in the past, the date range moves
  to its time span, unless you have set the range by hand.
- **Several datasets at once:** tick individual datasets or a whole category (e.g. Radar),
  enter a place, choose a date range and start the search.
  - Datasets whose extent or time span does not match are skipped without a network request.
    The rest are searched in parallel.
  - The result list shows all scenes with a *Dataset* column and can be sorted by
    date.
  - On download, each dataset writes its default bands at native resolution into its own
    subfolder.
  - Without ticks, the clicked row is used and you can choose any bands.
- **Result list:**
  - Per scene it shows *Clear %* (clear share within the area, computed in the background from
    the mask band), coverage and scene cloud cover, all sortable.
  - *Max. scenes per dataset* keeps the newest matches. The list
    tells you when the limit is reached.
  - The plugin asks before downloading more than 20 scenes, whole raw files, or an estimated
    5 GB or more.

| Provider | Collections | Examples |
|---|---|---|
| Microsoft Planetary Computer | 105 | Sentinel-1/2/3/5P, Landsat 1–9 (MSS from 1972), HLS, ASTER, MODIS (≈ 20 products), GOES, ALOS PALSAR, NAIP, Copernicus/ALOS/NASA DEM, WorldCover, IO LULC, JRC surface water, climate and ocean data |
| INPE Brazil Data Cube | 71 | CBERS-2/2B/4/4A (CCD, HRC, WFI, MUX, PAN, WPM), Amazonia-1, Resourcesat-1 (AWiFS, LISS-3), GOES-16/19, MODIS, Landsat/S2/S1 for Brazil, data cubes and mosaics |
| Digital Earth Africa | 89 | Sentinel-2 and Landsat 5–9 (SR, temperature), cloud-free GeoMAD composites, Sentinel-1, Sentinel-3/5P, water (WOfS), rainfall (CHIRPS), cropland, soils |
| Element84 Earth Search | 3 | Sentinel-2 L2A and Collection 1 |
| swisstopo | 5 | SWISSIMAGE 10 cm, swissALTI3D, swissSURFACE3D, swissEO Sentinel-2 |
| Thünen Institute, DLR EOC, LGLN Lower Saxony | 63 | crop types Germany (10 m), forest structure, grassland, Sentinel-5P/GOME-2 L3, World Settlement Footprint, orthophotos 20 cm Lower Saxony |
| EODC, KAGIS Carinthia | 48 | Austria: DTM/DSM 1 m, orthophotos 20 cm (Carinthia, Styria), ground motion, Sentinel-1 parameters |
| Paituli (Finland), Canadian Geospatial Data, CEDA | 250 | quarterly Sentinel-2 mosaics of Finland, forest inventory, HRDEM Canada 1 m, land cover, aerial photo series, CLMS land cover 10 m, EOCIS UK |
| WorldPop, GEO BON, Earth Genome, Astraea, KyFromAbove, OpenAerialMap | 293 | population per country, biodiversity and forest layers, annual Sentinel-2 mosaics, Alaska lidar, Umbra SAR, Kentucky imagery/DEM, NOAA emergency and OAM imagery |
| **Static catalogues** (bundled index) | 340 | ArcticDEM and REMA (2/10/32 m), OpenLandMap and EcoDataCube (soil, climate, vegetation, relief), GloFAS flood hazard, SPOT 4/5 Canada 2005–2010, Maxar and Planet disaster data, Capella SAR |

Some catalogues have no search API and consist only of JSON trees. `tools/build_static_index.py`
crawls them in advance, and the plugin then searches `data/static/*.items.json.gz` offline
(≈ 4 MB in total). Only the data files themselves come from the network. Event data (Maxar,
Planet) is as current as the last index run.

Raster products are read as subsets (COG/GeoTIFF); netCDF/HDF files are downloaded whole.

Not included is data that is only available through requester-pays S3, behind an account
(Planet NICFI), or as Zarr, JP2, tables or point clouds. Digital Earth Australia currently
blocks programmatic access with bot protection. Details and reasons are in
[tools/catalog_report.md](tools/catalog_report.md).

## How the data is produced

- **Subsets only:** the plugin reads just the part of each file that covers the area, via HTTP
  range requests (GDAL `/vsicurl/`).
- **Pixel-exact grids:** the target raster takes the CRS of most scenes and aligns exactly with
  their pixel grid. This includes the Landsat origin at 30 m + 15 m. Without a scene CRS the
  plugin falls back to:
  - UTM;
  - polar stereographic beyond 84° N / 80° S;
  - equal-area (LAEA) for very wide areas.

  Areas across the antimeridian are searched and clipped correctly.
- **Footprints:** scenes whose footprint does not touch the area are discarded. INPE returns
  such scenes because it only checks the bounding box.
- **NoData:** each file has one common NoData value. Masks without declared NoData (CBERS
  CMASK = 0, Landsat `qa_pixel` = 1) are converted. Days without valid pixels are skipped.
- **Clouds:** the clear share within the area is computed at full resolution from SCL,
  `qa_pixel` or CMASK. The COG overviews of class bands are averaged and therefore unusable.
- **Network failures:**
  - Retries on rate limits and server errors.
  - Re-signing when a Planetary Computer token expires (lifetime ≈ 45 min).
  - Resuming interrupted raw downloads.
- **File names and metadata:**
  - Each result is written as `<name>_<hash>.tif`, with a `.json` of the same name listing
    scenes, CRS, extent, resolution and NoData.
  - Identical settings reuse existing files; changed settings produce new ones.

## Notes on values

- **Sentinel-2 L2A**: since processing baseline 04.00 (25 Jan 2022), reflectance =
  (DN − 1000) / 10000; before that, DN / 10000.
- **Landsat C2 L2 SR**: reflectance = DN × 0.0000275 − 0.2.
- **Sentinel-1 RTC**: expert mode delivers linear backscatter; the Radar recipe adds dB.
- The default resampling is `nearest`, so class bands stay unchanged.

## Installation

Copy or link the `satellite_downloader` folder into the plugin directory of your QGIS profile:

```
Windows:  %APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\satellite_downloader
Linux:    ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/satellite_downloader
macOS:    ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/satellite_downloader
```

Then enable it under *Plugins → Manage and Install Plugins*.

Requires QGIS 3.34 or later. Tested with QGIS 3.44.2 / GDAL 3.11.3 (OSGeo4W, Windows).

## Project layout and tests

```
satellite_downloader/
  core/      catalogue, STAC, geometry, clouds, download pipeline, recipes, place search (no widgets)
  gui/       simple mode (dock), expert mode, styling, locator filter
  data/      generated catalogue (collections.json) and static indexes (static/)
  tasks.py   QgsTask wrappers around the core
tools/
  build_catalog.py       builds the catalogue from the live STAC APIs
  build_static_index.py  search index for static catalogues without an API
tests/
  test_core.py           offline tests of the logic
  test_catalog.py        generated catalogue, band mapping, normalisation (offline)
  test_static_index.py   static index search, footprints, antimeridian (offline)
  test_gui_usability.py  both modes driven with real keys and clicks (offline)
  test_gui_layout.py     window sizes with real fonts (QT_QPA_PLATFORM=windows only)
  test_live.py           against the live catalogues (SD_LIVE=1)
  test_gui_*.py          dialogs offscreen end to end (SD_LIVE=1)
  test_sensors_live.py   one real download per sensor family and provider (SD_LIVE=1)
  benchmark.py           timing of search, cloud estimate and download
  locations.py           core recipes at 20 places plus open sea
```

Run the tests with the Python that ships with QGIS, from the `tests` folder. On Windows
(OSGeo4W):

```
C:\OSGeo4W\bin\python-qgis.bat -m unittest test_core test_catalog test_static_index test_gui_usability
set QT_QPA_PLATFORM=windows && C:\OSGeo4W\bin\python-qgis.bat -m unittest test_gui_layout
set SD_LIVE=1 && C:\OSGeo4W\bin\python-qgis.bat -m unittest test_live test_gui_expert test_gui_simple
```

Live tests and the scripts write their output to `tests/_output/`.

## Code checks

The rules for ruff, mypy and basedpyright are in `pyproject.toml`. Lint and format checks
run on every commit once pre-commit is set up, together with a secret scan (gitleaks):

```
pre-commit install
```

The type checkers need the QGIS libraries, so they run with the QGIS Python (here with
[uv](https://docs.astral.sh/uv/)):

```
set PYTHONPATH=C:\OSGeo4W\apps\qgis\python
uvx mypy --python-executable C:\OSGeo4W\apps\Python312\python.exe
uvx basedpyright --pythonpath C:\OSGeo4W\apps\Python312\python.exe
```

basedpyright reports Qt typing artefacts as warnings; errors and mypy findings are real.

## Updating the catalogue and adding collections

The catalogue is generated from the live catalogues. For each collection,
`tools/build_catalog.py` fetches a sample item, checks that its files can be read without an
account, and reads resolution, NoData, band names, mask band and orbit grouping.

```
C:\OSGeo4W\bin\python-qgis.bat tools\build_catalog.py
```

The output is `satellite_downloader/data/collections.json` plus the report
[tools/catalog_report.md](tools/catalog_report.md).

- **Special cases:** what the automatic detection misses goes into `CURATED` in
  `satellite_downloader/core/catalog.py`, as hand-maintained entries that take precedence.
  Always set the NoData value there if the files don't declare one. Otherwise, empty tile
  edges in the mosaic cover valid pixels.
- **New providers:** a provider with a STAC API is added to `PROVIDERS` in `core/catalog.py`.
  Selection,
  exclusions and name prefixes per provider are in `PROVIDER_OPTIONS` in the generator.
- **Variable asset keys:** some keys change from item to item, such as sheet numbers or dates.
  The generator detects them from three sample items and replaces them with stable keys and
  a pattern.

Static catalogues (without a search API) are described in `SOURCES` in
`tools/build_static_index.py`: start URL, grouping into collections and a fixed category. They
can be re-indexed individually or together, for example to pick up recent disaster data:

```
C:\OSGeo4W\bin\python-qgis.bat tools\build_static_index.py maxar planetdisaster
```

The output is `data/static/<provider>.json` and `.items.json.gz` (at most 3 MB per provider),
plus the report [tools/static_index_report.md](tools/static_index_report.md).

## Data sources and licences

The satellite data belongs to the respective providers and is subject to their licences. The
plugin downloads it directly from them and shows the source and licence notice for each
dataset. Check the provider's terms before reusing data.

Some datasets are restricted:

- Maxar Open Data is CC BY-NC 4.0, so no commercial use.
- Some catalogues are marked "proprietary" (Capella, GloFAS, SPOT Canada).

The bundled indexes under `satellite_downloader/data/` contain only metadata from public
catalogues (footprints, dates, file URLs), no imagery.

Place search: [Nominatim](https://nominatim.org/), data © OpenStreetMap contributors (ODbL).

## Contributing

Bug reports, dataset requests and pull requests are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md). Report
security problems privately as described in [SECURITY.md](SECURITY.md).

## License

© 2026 Dennis Handy. Licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later). See [LICENSE](LICENSE).
