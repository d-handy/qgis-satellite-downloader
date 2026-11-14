"""End-to-end run of the expert dialog (offscreen). Run with SD_LIVE=1."""

import os
import unittest

import qgis_app

app = qgis_app.start()

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRectangle,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, Qt, QTimer

from satellite_downloader.gui.expert_dialog import DownloaderDialog
from satellite_downloader.gui.results_panel import COL_CHECK, COL_CLEAR, COL_SENSOR


class _Bar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, *args):
        self.messages.append(args)


class FakeIface:
    def __init__(self):
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.resize(800, 600)
        self.canvas.setExtent(QgsRectangle(771000, 6605000, 776000, 6610000))
        self.bar = _Bar()

    def mainWindow(self):
        return None

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar


def wait_until(condition, timeout_s=300):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(200)
    QTimer.singleShot(timeout_s * 1000, loop.quit)
    loop.exec()
    timer.stop()


@unittest.skipUnless(qgis_app.LIVE, "set SD_LIVE=1 for tests against the live services")
class ExpertDialogTest(unittest.TestCase):
    def test_search_clear_sky_and_download(self):
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        iface = FakeIface()
        dlg = DownloaderDialog(iface)
        dlg.max_items_spin.setValue(3)
        dlg.out_dir.setFilePath(os.path.join(qgis_app.OUTPUT, "gui"))
        dlg.start_search()
        wait_until(lambda: dlg.search_task is None)
        self.assertGreater(dlg.table.rowCount(), 0, dlg.log_view.toPlainText())

        wait_until(lambda: dlg.clear_task is None)
        values = [
            dlg.table.item(r, COL_CLEAR).data(0) for r in range(dlg.table.rowCount())
        ]
        self.assertTrue(all(v is not None for v in values), dlg.log_view.toPlainText())

        dlg.table.sortItems(COL_CLEAR)  # selection must follow the rows
        dlg.start_download()
        wait_until(lambda: dlg.download_task is None)
        log = dlg.log_view.toPlainText()
        self.assertIn("Done:", log)
        layers = [lyr.name() for lyr in QgsProject.instance().mapLayers().values()]
        self.assertTrue(
            any(name.startswith("sentinel-2-l2a_") for name in layers), layers
        )
        dlg.close()


class CollectionPickerTest(unittest.TestCase):
    def test_search_and_area_filter(self):
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        dlg = DownloaderDialog(FakeIface())  # canvas shows Cologne
        picker = dlg.collection_picker
        total = picker.count()
        self.assertGreater(total, 250)

        picker.search.setText("modis ndvi")
        keys = {leaf.data(0, Qt.ItemDataRole.UserRole) for leaf in picker._leaves()}
        self.assertIn("pc:modis-13Q1-061", keys)
        self.assertLess(len(keys), 15)

        picker.search.setText("")
        picker.only_here.setChecked(True)
        keys = {leaf.data(0, Qt.ItemDataRole.UserRole) for leaf in picker._leaves()}
        self.assertNotIn("pc:naip", keys)  # USA only
        self.assertNotIn("deafrica:s2_l2a", keys)  # Africa only
        self.assertIn("s2-l2a", keys)
        self.assertLess(len(keys), total)

        picker.setCurrentKey("pc:landsat-c2-l1")
        self.assertEqual(dlg.collection.key, "pc:landsat-c2-l1")
        self.assertEqual(dlg.date_to.date().year(), 2013)  # moved into the data period
        dlg.close()


@unittest.skipUnless(qgis_app.LIVE, "set SD_LIVE=1 for tests against the live services")
class MultiSensorSearchTest(unittest.TestCase):
    def test_place_category_search_and_download(self):
        from qgis.PyQt.QtCore import QDate

        from satellite_downloader.core import selection

        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        dlg = DownloaderDialog(FakeIface())
        dlg.out_dir.setFilePath(os.path.join(qgis_app.OUTPUT, "multi"))

        # place search sets the area
        dlg.place_edit.setText("50.94, 6.95")
        dlg.search_place()
        box = dlg._area_bbox()
        self.assertTrue(box[0] < 6.95 < box[2] and box[1] < 50.94 < box[3], box)

        # tick the whole "Radar" category
        picker = dlg.collection_picker
        category = picker.categoryItem("Radar")
        self.assertIsNotNone(category)
        category.setCheckState(0, Qt.CheckState.Checked)
        self.assertGreater(len(picker.checkedKeys()), 2)
        self.assertGreater(len(dlg._selection()), 1)
        self.assertFalse(dlg.band_list.isEnabled())

        dlg.date_from.setDate(QDate(2026, 8, 20))
        dlg.date_to.setDate(QDate(2026, 9, 15))
        dlg.max_items_spin.setValue(3)
        dlg.start_search()
        wait_until(lambda: dlg.search_task is None)
        log = dlg.log_view.toPlainText()
        self.assertIn("skipped", log)  # e.g. Africa-only and ended collections
        sensors = {it[selection.COLLECTION_KEY] for it in dlg.items}
        self.assertGreaterEqual(len(sensors), 2, log)
        self.assertEqual(dlg.table.item(0, COL_SENSOR).text() != "", True)

        # keep one scene per sensor, at most two sensors
        keep = {}
        for row in range(dlg.table.rowCount()):
            uid = dlg.table.item(row, COL_CHECK).data(Qt.ItemDataRole.UserRole)
            key = dlg.items_by_uid[uid][selection.COLLECTION_KEY]
            wanted = key not in keep and len(keep) < 2
            if wanted:
                keep[key] = uid
            dlg.table.item(row, COL_CHECK).setCheckState(
                Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked
            )
        dlg.start_download()
        wait_until(lambda: dlg.download_task is None, 600)
        log = dlg.log_view.toPlainText()
        self.assertIn("Done:", log, log)
        self.assertIn("from 2 datasets", log)

        # clearing the ticks returns to the single-sensor view
        picker.clear_checked()
        self.assertEqual(len(dlg._selection()), 1)
        self.assertTrue(dlg.band_list.isEnabled())
        dlg.close()


if __name__ == "__main__":
    unittest.main()
