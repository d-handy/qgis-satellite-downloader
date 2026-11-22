# Installation

Requires QGIS 3.34 or later. Tested with QGIS 3.44.2 / GDAL 3.11.3 (OSGeo4W, Windows).

## From a release

Download `satellite_downloader-<version>.zip` from the
[releases page](https://github.com/d-handy/qgis-satellite-downloader/releases) and choose
*Plugins → Manage and Install Plugins → Install from ZIP*.

## From the source

Copy or link the `satellite_downloader` folder into the plugin directory of your QGIS profile:

```
Windows:  %APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\satellite_downloader
Linux:    ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/satellite_downloader
macOS:    ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/satellite_downloader
```

Then enable it under *Plugins → Manage and Install Plugins*.
