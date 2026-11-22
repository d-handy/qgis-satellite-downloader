# qgis-satellite-downloader <img src="docs/assets/logo/qgis-satellite-downloader-logo.svg" align="right" height="139" alt="QGIS Satellite Downloader logo" />

A QGIS plugin that loads satellite data for any place on Earth. You don't need an account,
an API key or any extra Python packages: it runs on QGIS and the GDAL that ships with it.

- **Simple mode:** enter a place, choose a purpose (current image, radar, or elevation and
  terrain) and click *Load*. The plugin picks the scene, bands, resolution and coordinate
  reference system on its own.
- **Expert mode:** free access to **1267 collections from 27 providers**, among them
  Microsoft Planetary Computer, INPE Brazil Data Cube, Digital Earth Africa, Element84 and
  swisstopo. You choose bands, date range, resolution, CRS and resampling yourself.

The plugin reads just the part of each file that covers the area, via HTTP range requests
(GDAL `/vsicurl/`).

## Installation

Download `satellite_downloader-<version>.zip` from the
[releases page](https://github.com/d-handy/qgis-satellite-downloader/releases) and choose
*Plugins → Manage and Install Plugins → Install from ZIP*. Requires QGIS 3.34 or later.

## Documentation

The documentation is at **https://d-handy.github.io/qgis-satellite-downloader/**:

- [Installation](https://d-handy.github.io/qgis-satellite-downloader/getting-started/installation.html) and [simple mode](https://d-handy.github.io/qgis-satellite-downloader/getting-started/simple-mode.html)
- [Expert mode](https://d-handy.github.io/qgis-satellite-downloader/user-guide/expert-mode.html) and [how the data is produced](https://d-handy.github.io/qgis-satellite-downloader/user-guide/how-data-is-produced.html)
- [Providers](https://d-handy.github.io/qgis-satellite-downloader/reference/providers.html) and [data sources and licences](https://d-handy.github.io/qgis-satellite-downloader/reference/licences.html)
- [Testing and code checks](https://d-handy.github.io/qgis-satellite-downloader/development/testing.html) and [updating the catalogue](https://d-handy.github.io/qgis-satellite-downloader/development/catalogue.html)

Its sources are in [`docs/source/`](docs/source/); build them locally with
`uv run --only-group docs sphinx-build -W -b html docs/source docs/build/html`. Release notes
are in [CHANGELOG.md](CHANGELOG.md) and how to contribute in [CONTRIBUTING.md](CONTRIBUTING.md),
together with the [code of conduct](CODE_OF_CONDUCT.md). Report security problems privately as
described in [SECURITY.md](SECURITY.md).

## Data and licences

The satellite data belongs to the respective providers and is subject to their licences. The
plugin shows the source and licence notice for each dataset; check the provider's terms before
reusing data. Place search: [Nominatim](https://nominatim.org/), data © OpenStreetMap
contributors (ODbL).

## Citing

Citation metadata for the current release are in [CITATION.cff](CITATION.cff).

## License

© 2026 Dennis Handy. Licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later). See [LICENSE](LICENSE).
