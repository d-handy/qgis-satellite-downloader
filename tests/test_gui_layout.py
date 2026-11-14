"""Layout of both windows with real fonts. Run on its own, in a separate process:

    set QT_QPA_PLATFORM=windows
    python-qgis.bat -m unittest test_gui_layout

Other platforms are skipped: offscreen has no usable fonts here. Windows are
laid out without appearing on screen.
"""

import unittest

import qgis_app

app = qgis_app.start()

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QPoint, Qt
from test_gui_simple import FakeIface

from satellite_downloader.core import selection
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY
from satellite_downloader.gui.expert_dialog import DownloaderDialog
from satellite_downloader.gui.results_panel import COL_CLEAR
from satellite_downloader.gui.simple_dock import SimpleDock

MAX_DIALOG_HEIGHT = 800
MAX_DOCK_WIDTH = 340


def settle():
    for _ in range(5):  # nested layouts settle over several event rounds
        app.processEvents()


def show(widget):
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    settle()
    return widget


@unittest.skipUnless(app.platformName() == "windows", "needs real fonts")
class ExpertLayoutTest(unittest.TestCase):
    def setUp(self):
        self.dlg = show(DownloaderDialog(FakeIface()))
        self.addCleanup(self.dlg.close)

    def test_fits_small_screens(self):
        self.assertLess(self.dlg.minimumSizeHint().height(), MAX_DIALOG_HEIGHT)
        self.assertLessEqual(self.dlg.height(), MAX_DIALOG_HEIGHT)

    def _assert_no_overlap(self):
        picker = self.dlg.collection_picker
        tree_bottom = picker.tree.mapTo(self.dlg, QPoint(0, picker.tree.height())).y()
        label_top = picker.checked_label.mapTo(self.dlg, QPoint(0, 0)).y()
        self.assertLessEqual(tree_bottom, label_top)

    def test_list_and_tick_row_do_not_overlap(self):
        self._assert_no_overlap()

    def test_no_overlap_with_a_long_note(self):
        self.dlg.collection_picker.setCheckedKeys(["s2-l2a", "s1-rtc", "s1-grd"])
        settle()
        self._assert_no_overlap()

    def test_clear_sky_column_is_visible(self):
        props = {"datetime": "2026-01-01T10:00:00Z", "eo:cloud_cover": 12.5}
        self.dlg._set_items(
            [
                selection.tag(
                    {"id": f"scene{n}", "properties": props, "assets": {}},
                    COLLECTIONS_BY_KEY["s2-l2a"],
                )
                for n in range(3)
            ]
        )
        settle()
        table = self.dlg.table
        right = table.columnViewportPosition(COL_CLEAR) + table.columnWidth(COL_CLEAR)
        self.assertLessEqual(right, table.viewport().width())


@unittest.skipUnless(app.platformName() == "windows", "needs real fonts")
class DockLayoutTest(unittest.TestCase):
    def test_long_folder_path_does_not_widen_the_dock(self):
        QgsSettings().setValue(
            "SatelliteDownloader/out_dir",
            "C:\\" + "\\".join(["averylongfoldername"] * 12),
        )
        dock = SimpleDock(FakeIface(), lambda: None)
        self.addCleanup(dock.cleanup)
        show(dock)
        body = dock.widget().widget()
        self.assertLessEqual(body.minimumSizeHint().width(), MAX_DOCK_WIDTH)


if __name__ == "__main__":
    unittest.main()
