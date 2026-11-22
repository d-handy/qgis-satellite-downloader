# Expert mode

*Raster → Satellite Downloader → Expert mode …* gives free access to **1267 collections from
27 providers** (as of 17 Sep 2026; see [Providers](../reference/providers.md)). You choose
bands, date range, resolution, CRS and resampling yourself.

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
