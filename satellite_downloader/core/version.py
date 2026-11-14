"""The plugin version, read from ``metadata.txt``.

QGIS takes the version from ``metadata.txt``, so that file is the one place it
is written; the User-Agent and the result sidecars read it from there.
"""

from __future__ import annotations

import configparser
from pathlib import Path

METADATA_FILE = Path(__file__).resolve().parent.parent / "metadata.txt"


def read_version(path: Path = METADATA_FILE) -> str:
    """``version`` from the ``[general]`` section, "unknown" if it is missing."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser.get("general", "version", fallback="unknown").strip()


PLUGIN_VERSION = read_version()
