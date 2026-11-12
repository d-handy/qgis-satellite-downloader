# Contributing

Bug reports, missing datasets and pull requests are welcome. Please follow the
[code of conduct](CODE_OF_CONDUCT.md). Security problems go through
[SECURITY.md](SECURITY.md), not public issues.

## Reporting a problem

Use the issue forms: *Bug report*, *Dataset request* or *Idea*. For a bug, the most useful
details are the QGIS version, the mode (simple or expert), the dataset, the place, and the
lines from the QGIS log: *View → Panels → Log Messages*, tab "Satellite Downloader".

## Development setup

The plugin runs on the Python that ships with QGIS and needs no extra packages.

1. Clone the repository.
2. Link the `satellite_downloader` folder into your QGIS profile instead of copying it, so
   changes take effect after a plugin reload. On Windows (in an administrator prompt, or
   with developer mode enabled):

   ```
   mklink /D "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\satellite_downloader" "C:\path\to\qgis-satellite-downloader\satellite_downloader"
   ```

   On Linux and macOS, use `ln -s` with the plugin paths from the README.
3. Enable the plugin in QGIS. The *Plugin Reloader* plugin saves restarts.
4. Install the commit hooks (ruff and a secret scan): `pre-commit install`.

## Tests and checks

Run the offline tests from the `tests` folder with the QGIS Python before opening a pull
request; on Windows (OSGeo4W):

```
C:\OSGeo4W\bin\python-qgis.bat -m unittest test_core test_catalog test_static_index test_offline_io test_gui_usability
```

Changes to downloads, recipes or catalogue entries should also pass the live tests
(`set SD_LIVE=1`, see the README). The README section *Code checks* shows how to run mypy and
basedpyright; both should report no errors.

New behaviour needs a test. Interface text is English; keep it short and say what the user
can do next.

## Adding a dataset

- **Collection from a provider that is already included:** usually nothing to do. Rebuild
  the catalogue with `tools/build_catalog.py`; if the automatic detection gets resolution,
  bands or NoData wrong, add a hand-maintained entry to `CURATED` in
  `satellite_downloader/core/catalog.py`.
- **New STAC API:** add the provider to `PROVIDERS` in `core/catalog.py` and its selection
  options to `PROVIDER_OPTIONS` in `tools/build_catalog.py`.
- **Static catalogue without a search API:** describe it in `SOURCES` in
  `tools/build_static_index.py` and build its index.

The plugin only includes data that can be downloaded without an account. Check the
provider's licence and mention it in the pull request.

## Pull requests

- One topic per pull request, on a branch of its own.
- Describe what changes for the user and how you tested it.
- Commit the regenerated `collections.json` or static index together with the generator
  change that produced it.
