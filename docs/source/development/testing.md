# Testing and code checks

## Project layout

```
satellite_downloader/
  core/      catalogue, STAC, geometry, clouds, download pipeline, recipes, place search (no widgets)
  gui/       simple mode (dock), expert mode, styling, locator filter
  data/      generated catalogue (collections.json) and static indexes (static/)
  tasks.py   QgsTask wrappers around the core
docs/
  source/    this documentation (Sphinx, MyST Markdown)
  assets/    logos and screenshots; see "Logos and screenshots"
tools/
  build_catalog.py       builds the catalogue from the live STAC APIs
  build_static_index.py  search index for static catalogues without an API
  package_plugin.py      builds the installable plugin ZIP (dist/)
tests/
  test_core.py           offline tests of the logic
  test_catalog.py        generated catalogue, band mapping, normalisation (offline)
  test_static_index.py   static index search, footprints, antimeridian (offline)
  test_offline_io.py     GDAL reads, cloud estimate, raw downloads via a local server (offline)
  test_download_plan.py  download jobs and size estimate (offline)
  test_package.py        release ZIP contents and version tag (offline)
  test_gui_usability.py  both modes driven with real keys and clicks (offline)
  test_gui_layout.py     window sizes with real fonts (QT_QPA_PLATFORM=windows only)
  test_live.py           against the live catalogues (SD_LIVE=1)
  test_gui_*.py          dialogs offscreen end to end (SD_LIVE=1)
  test_sensors_live.py   one real download per sensor family and provider (SD_LIVE=1)
  benchmark.py           timing of search, cloud estimate and download
  locations.py           core recipes at 20 places plus open sea
```

## Running the tests

Run the tests with the Python that ships with QGIS, from the `tests` folder. On Windows
(OSGeo4W):

```
C:\OSGeo4W\bin\python-qgis.bat -m unittest test_core test_catalog test_static_index test_offline_io test_download_plan test_gui_usability test_package
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
