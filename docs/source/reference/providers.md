# Providers

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
[tools/catalog_report.md](https://github.com/d-handy/qgis-satellite-downloader/blob/main/tools/catalog_report.md).
