"""End-to-end run of the simple mode (offscreen). Run with SD_LIVE=1."""

import os
import unittest

import qgis_app

app = qgis_app.start()

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtWidgets import QMainWindow

from satellite_downloader.core import places
from satellite_downloader.gui.locator import PlaceLocatorFilter
from satellite_downloader.gui.simple_dock import SimpleDock
from satellite_downloader.plugin import SatelliteDownloaderPlugin


class FakeIface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.resize(800, 600)
        self.canvas.setExtent(QgsRectangle(771000, 6605000, 776000, 6610000))
        self.filters, self.docks, self.menu = [], [], []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return None

    def addRasterToolBarIcon(self, action):
        self.menu.append(action)

    def removeRasterToolBarIcon(self, action):
        pass

    def addPluginToRasterMenu(self, _menu, action):
        self.menu.append(action)

    def removePluginRasterMenu(self, _menu, action):
        pass

    def registerLocatorFilter(self, f):
        self.filters.append(f)

    def deregisterLocatorFilter(self, f):
        self.filters.remove(f)

    def addDockWidget(self, _area, dock):
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        self.docks.remove(dock)


def wait_until(condition, timeout_s=300):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(200)
    QTimer.singleShot(timeout_s * 1000, loop.quit)
    loop.exec()
    timer.stop()


def group_layers(name_part):
    root = QgsProject.instance().layerTreeRoot()
    for group in root.findGroups():
        if name_part in group.name():
            return group.name(), [n.layer() for n in group.findLayers()]
    return None, []


class LocatorTest(unittest.TestCase):
    def test_single_offline_result_and_trigger(self):
        opened = []
        f = PlaceLocatorFilter(opened.append)
        results = []
        f.resultFetched.connect(results.append)
        f.fetchResults("Atacama", None, None)
        self.assertEqual(len(results), 1)
        f.triggerResult(results[0])
        self.assertEqual(opened, ["Atacama"])


@unittest.skipUnless(qgis_app.LIVE, "set SD_LIVE=1 for tests against the live services")
class SimpleDockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QgsProject.instance().clear()
        QgsSettings().setValue(
            "SatelliteDownloader/out_dir", os.path.join(qgis_app.OUTPUT, "simple")
        )
        cls.iface = FakeIface()
        cls.dock = SimpleDock(cls.iface, lambda: None)
        cls.dock.radius.setValue(5.0)  # the last value is remembered between runs

    def run_recipe(self, key):
        for button in self.dock.recipe_group.buttons():
            button.setChecked(button.property("recipe") == key)
        self.dock.load()
        wait_until(lambda: self.dock.recipe_task is None, 600)
        return self.dock.info.toPlainText()

    def test_1_place_search_offers_choices_and_point_becomes_circle(self):
        self.dock.search("Atacama")
        wait_until(lambda: self.dock.geocode_task is None, 60)
        self.assertIsNotNone(self.dock.place, self.dock.status.text())
        self.assertGreaterEqual(self.dock.results.count(), 1)
        self.assertEqual(self.dock.area.kind, "circle")  # Nominatim returns a point
        self.assertAlmostEqual(self.dock.area.width_km, 10.0)
        self.dock.radius.setValue(3.0)
        self.assertAlmostEqual(self.dock.area.width_km, 6.0)

    def test_2_current_image(self):
        info = self.run_recipe("current")
        self.assertIn("Clear sky", info, info)
        name, layers = group_layers("Current image")
        self.assertIsNotNone(name)
        self.assertIsInstance(layers[0].renderer(), QgsMultiBandColorRenderer)
        self.assertTrue(layers[0].temporalProperties().isActive())
        self.assertIsNone(self.dock.preview_layer_id)  # preview replaced by the result
        expected = "Copernicus Sentinel" if "Sentinel-2" in info else "Source"
        self.assertIn(expected, self.dock.last_attribution)

    def test_3_radar(self):
        info = self.run_recipe("radar")
        self.assertIn("Sentinel-1", info, info)
        _, layers = group_layers("Radar")
        self.assertIsInstance(layers[0].renderer(), QgsSingleBandGrayRenderer)
        self.assertTrue(layers[0].source().endswith("_dB.tif"))

    def test_4_dem(self):
        info = self.run_recipe("dem")
        self.assertIn("Copernicus DEM", info, info)
        _, layers = group_layers("Elevation and terrain")
        names = [lyr.name() for lyr in layers]
        self.assertEqual(names, ["Hillshade", "Elevation (m)", "Slope (°)"])
        self.assertIsInstance(layers[1].renderer(), QgsSingleBandPseudoColorRenderer)

    def test_5_coordinates_without_geocoder(self):
        self.dock.search("78.22, 15.6")
        self.assertEqual(self.dock.place.kind, "coordinate")
        self.assertAlmostEqual(self.dock.place.lat, 78.22)

    def test_6_large_place_note(self):
        place = places.Place(
            "Svalbard", 17.0, 78.5, (74.0, 81.0, 10.0, 35.0), "boundary"
        )
        self.dock.set_place(place)
        self.assertIn("using a circle", self.dock.area.note)
        self.assertTrue(self.dock.cb_whole.isVisible() or not self.dock.isVisible())


class PluginLifecycleTest(unittest.TestCase):
    def test_init_open_unload(self):
        iface = FakeIface()
        plugin = SatelliteDownloaderPlugin(iface)
        plugin.initGui()
        self.assertEqual(len(iface.filters), 1)
        plugin.show_dock()
        self.assertEqual(len(iface.docks), 1)
        plugin.unload()
        self.assertEqual(iface.filters, [])
        self.assertEqual(iface.docks, [])


if __name__ == "__main__":
    unittest.main()
