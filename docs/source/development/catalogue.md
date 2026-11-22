# Catalogue and collections

The catalogue is generated from the live catalogues. For each collection,
`tools/build_catalog.py` fetches a sample item, checks that its files can be read without an
account, and reads resolution, NoData, band names, mask band and orbit grouping.

```
C:\OSGeo4W\bin\python-qgis.bat tools\build_catalog.py
```

The output is `satellite_downloader/data/collections.json` plus the report
[tools/catalog_report.md](https://github.com/d-handy/qgis-satellite-downloader/blob/main/tools/catalog_report.md).

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
plus the report [tools/static_index_report.md](https://github.com/d-handy/qgis-satellite-downloader/blob/main/tools/static_index_report.md).
