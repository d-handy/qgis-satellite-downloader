# Changelog

All notable changes to this plugin. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/). The version itself lives in
`satellite_downloader/metadata.txt`; the short `changelog=` line there is what QGIS shows
in the plugin manager, so keep both in step when releasing.

## [Unreleased]

### Changed

- Output file names now include a pipeline version, and each result's `.json` records the
  plugin and GDAL version. Results made with earlier versions are therefore downloaded
  again once instead of being reused.

### Fixed

- One unreachable file no longer stops a raw download; it counts as a failure, is retried,
  and the other files still arrive.
- "Current image" no longer fails when one source (e.g. Landsat) is down: the remaining
  sources decide, and a note names the source that was left out. If no source answers, the
  message says so and suggests trying again later.
- Signed Planetary Computer links are correct for files whose address already has a query.
- One token request per provider instead of one per acquisition (up to 54 per Load before).
- The size estimate counts files the way the download groups them (day plus orbit or path).
- Preview files no longer pile up in the temporary folder.

## [0.4.0]

### Changed

- English user interface and catalogue.
- Results are saved under `SatelliteData/` in the project folder; derived files are named
  `_hillshade.tif` and `_slope.tif`.

## [0.3.0]

### Changed

- A typed place is used when loading or searching; Enter no longer clears the ticks.
- The display follows the ticked datasets; all native resolutions are offered.
- Notice when the scene limit is reached, hints for empty results, readable messages.
- Scrolling settings, folder choice, more coordinate formats.

## [0.2.0]

### Added

- Simple mode: place, purpose, load.
- Recipes "Current image", "Radar" and "Elevation"; clear sky inside the area.
- Orbit-aware mosaics, antimeridian and polar support.
- Retries and resumable downloads, pixel-exact grids.

[Unreleased]: https://github.com/d-handy/qgis-satellite-downloader/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/d-handy/qgis-satellite-downloader/releases/tag/v0.4.0
[0.3.0]: https://github.com/d-handy/qgis-satellite-downloader/releases/tag/v0.3.0
[0.2.0]: https://github.com/d-handy/qgis-satellite-downloader/releases/tag/v0.2.0
