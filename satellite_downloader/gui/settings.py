"""Settings both modes share: the QgsSettings key prefix and the default folder."""

from __future__ import annotations

import os

SETTINGS_PREFIX = "SatelliteDownloader/"


def default_out_dir() -> str:
    """Download folder used before the user chooses one (and without a project)."""
    return os.path.join(os.path.expanduser("~"), "SatelliteDownloads")
