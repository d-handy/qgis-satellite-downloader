# Simple mode: place → purpose → load

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
