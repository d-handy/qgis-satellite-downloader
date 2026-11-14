"""Build the installable plugin ZIP.

QGIS installs a plugin from a ZIP whose single top-level folder is the plugin
folder (*Plugins -> Manage and Install Plugins -> Install from ZIP*); the
plugins.qgis.org upload expects the same layout. This script packs
``satellite_downloader/`` that way, without caches and editor leftovers, and
names the file after the version in ``metadata.txt``:

    python tools/package_plugin.py            -> dist/satellite_downloader-0.4.0.zip
    python tools/package_plugin.py --check-tag v0.4.0

It needs only the standard library, so any Python 3.10+ can run it.
"""

from __future__ import annotations

import argparse
import configparser
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "satellite_downloader"
DIST_DIR = ROOT / "dist"

# Folders and files that never belong in a release.
EXCLUDED_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".part", ".orig", ".rej", ".bak"}
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db"}
# QGIS refuses a plugin without these.
REQUIRED_FILES = ("__init__.py", "metadata.txt", "LICENSE")
REQUIRED_METADATA = ("name", "qgisMinimumVersion", "description", "version", "author")


class PackageError(Exception):
    """The plugin folder cannot be packed as it is."""


def read_metadata(plugin_dir: Path = PLUGIN_DIR) -> dict[str, str]:
    """The ``[general]`` section of ``metadata.txt``; raises if a required key is missing."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep camelCase keys
    if not parser.read(plugin_dir / "metadata.txt", encoding="utf-8"):
        raise PackageError(f"{plugin_dir / 'metadata.txt'} not found")
    if not parser.has_section("general"):
        raise PackageError("metadata.txt has no [general] section")
    meta = dict(parser.items("general"))
    missing = [key for key in REQUIRED_METADATA if not meta.get(key, "").strip()]
    if missing:
        raise PackageError(f"metadata.txt lacks: {', '.join(missing)}")
    return meta


def plugin_files(plugin_dir: Path = PLUGIN_DIR) -> Iterator[Path]:
    """Every file to ship, in a stable order."""
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_dir)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        if path.suffix in EXCLUDED_SUFFIXES or path.name in EXCLUDED_NAMES:
            continue
        yield path


def build(plugin_dir: Path = PLUGIN_DIR, dist_dir: Path = DIST_DIR) -> Path:
    """Write ``<dist_dir>/<folder>-<version>.zip`` and return its path."""
    for name in REQUIRED_FILES:
        if not (plugin_dir / name).is_file():
            raise PackageError(f"{name} is missing in {plugin_dir}")
    version = read_metadata(plugin_dir)["version"].strip()
    dist_dir.mkdir(parents=True, exist_ok=True)
    target = dist_dir / f"{plugin_dir.name}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in plugin_files(plugin_dir):
            arcname = Path(plugin_dir.name) / path.relative_to(plugin_dir)
            archive.write(path, arcname.as_posix())
    return target


def check_tag(tag: str, plugin_dir: Path = PLUGIN_DIR) -> None:
    """Raise unless ``tag`` is ``v`` plus the version in ``metadata.txt``."""
    version = read_metadata(plugin_dir)["version"].strip()
    if tag != f"v{version}":
        raise PackageError(
            f"tag {tag!r} does not match metadata.txt version {version!r} "
            f"(expected 'v{version}')"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=DIST_DIR, help="output folder")
    parser.add_argument(
        "--check-tag", metavar="TAG", help="fail unless TAG equals v<version>"
    )
    args = parser.parse_args(argv)
    try:
        if args.check_tag:
            check_tag(args.check_tag)
        target = build(dist_dir=args.dist)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
