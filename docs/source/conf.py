"""Sphinx configuration for the plugin documentation.

Pages are written in Markdown (MyST). The plugin needs QGIS and GDAL, which the
docs build doesn't have, so nothing here imports it; the version comes from
metadata.txt.
"""

import configparser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]

_metadata = configparser.ConfigParser(interpolation=None)
_metadata.read(ROOT_DIR / "satellite_downloader" / "metadata.txt", encoding="utf-8")

# -- Project information -----------------------------------------------------

project = "Satellite Downloader"
author = "Dennis Handy"
copyright = f"2026, {author}"
release = _metadata["general"]["version"]
version = release

# -- General configuration ---------------------------------------------------

extensions = ["myst_parser", "sphinx_rtd_theme"]

source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "build"]

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

# -- HTML output -------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_title = f"Satellite Downloader {release}"
html_logo = "../assets/logo/qgis-satellite-downloader-icon.svg"
html_theme_options = {"navigation_depth": 3}
html_context = {
    "display_github": True,
    "github_user": "d-handy",
    "github_repo": "qgis-satellite-downloader",
    "github_version": "main",
    "conf_py_path": "/docs/source/",
}
