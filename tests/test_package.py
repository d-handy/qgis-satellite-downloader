"""Offline checks of the release ZIP (tools/package_plugin.py).

Needs no QGIS: the packaging script uses only the standard library.
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import package_plugin  # noqa: E402

METADATA = """[general]
name=Demo
qgisMinimumVersion=3.34
description=Demo plugin
version=1.2.3
author=Someone
"""


def make_plugin(root: Path, metadata: str = METADATA) -> Path:
    plugin = root / "demo_plugin"
    (plugin / "core" / "__pycache__").mkdir(parents=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "LICENSE").write_text("GPL", encoding="utf-8")
    (plugin / "metadata.txt").write_text(metadata, encoding="utf-8")
    (plugin / "core" / "logic.py").write_text("X = 1\n", encoding="utf-8")
    (plugin / "core" / "__pycache__" / "logic.cpython-312.pyc").write_bytes(b"\0")
    (plugin / "core" / "stale.pyc").write_bytes(b"\0")
    (plugin / "core" / "download.part").write_bytes(b"\0")
    return plugin


class PackageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zip_has_one_top_folder_and_no_caches(self):
        plugin = make_plugin(self.root)
        target = package_plugin.build(plugin, self.root / "dist")
        self.assertEqual(target.name, "demo_plugin-1.2.3.zip")
        with zipfile.ZipFile(target) as archive:
            names = sorted(archive.namelist())
        self.assertEqual(
            names,
            [
                "demo_plugin/LICENSE",
                "demo_plugin/__init__.py",
                "demo_plugin/core/logic.py",
                "demo_plugin/metadata.txt",
            ],
        )

    def test_missing_metadata_key_is_reported(self):
        plugin = make_plugin(self.root, METADATA.replace("author=Someone\n", ""))
        with self.assertRaisesRegex(package_plugin.PackageError, "author"):
            package_plugin.build(plugin, self.root / "dist")

    def test_missing_license_is_reported(self):
        plugin = make_plugin(self.root)
        (plugin / "LICENSE").unlink()
        with self.assertRaisesRegex(package_plugin.PackageError, "LICENSE"):
            package_plugin.build(plugin, self.root / "dist")

    def test_tag_must_match_version(self):
        plugin = make_plugin(self.root)
        package_plugin.check_tag("v1.2.3", plugin)
        for tag in ("1.2.3", "v1.2.4"):
            with self.assertRaises(package_plugin.PackageError):
                package_plugin.check_tag(tag, plugin)

    def test_real_plugin_packs(self):
        target = package_plugin.build(dist_dir=self.root / "dist")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        self.assertIn("satellite_downloader/metadata.txt", names)
        self.assertIn("satellite_downloader/data/collections.json", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])


if __name__ == "__main__":
    unittest.main()
