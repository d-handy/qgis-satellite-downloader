def classFactory(iface):
    from .plugin import SatelliteDownloaderPlugin

    return SatelliteDownloaderPlugin(iface)
