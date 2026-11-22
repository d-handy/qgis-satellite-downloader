# How the data is produced

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
  - Identical settings reuse existing files; changed settings produce new ones. So does
    a plugin update that changes how files are made, so an old result is never reused
    as if it were current.
  - The `.json` also records the plugin and GDAL version that made the file.

## Notes on values

- **Sentinel-2 L2A**: since processing baseline 04.00 (25 Jan 2022), reflectance =
  (DN − 1000) / 10000; before that, DN / 10000.
- **Landsat C2 L2 SR**: reflectance = DN × 0.0000275 − 0.2.
- **Sentinel-1 RTC**: expert mode delivers linear backscatter; the Radar recipe adds dB.
- The default resampling is `nearest`, so class bands stay unchanged.
