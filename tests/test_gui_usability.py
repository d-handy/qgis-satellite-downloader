"""Usability guarantees of both modes, offline: what the UI shows is what runs.

Layout sizes are not asserted here: the offscreen platform has no usable fonts.
"""

import contextlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import qgis_app

app = qgis_app.start()

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsTask,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QPushButton
from test_gui_simple import FakeIface, wait_until

from satellite_downloader.core import places, recipes, selection, stac
from satellite_downloader.core.catalog import COLLECTIONS_BY_KEY
from satellite_downloader.core.text import LOG_HINT, LOG_TAG
from satellite_downloader.gui import (
    download_plan,
    expert_dialog,
    messages,
    results_panel,
    simple_dock,
)
from satellite_downloader.gui.collection_picker import period
from satellite_downloader.gui.expert_dialog import DownloaderDialog
from satellite_downloader.gui.simple_dock import SimpleDock
from satellite_downloader.tasks import RecipeTask, SearchTask

KOELN = places.Place(
    "Cologne, NRW", 6.96, 50.94, (50.83, 51.08, 6.77, 7.16), "boundary"
)
BONN = places.Place("Bonn, NRW", 7.10, 50.73, (50.63, 50.77, 7.02, 7.21), "boundary")


def fake_geocode(found):
    """A GeocodeTask replacement that answers without the network."""

    class Task(QgsTask):
        def __init__(self, query):
            super().__init__("fake geocode", QgsTask.Flag.CanCancel)
            self.query = query
            self.places = list(found)

        def run(self):
            return True

        def failure(self):
            return None

    return Task


class RecordingRecipeTask(QgsTask):
    """Stands in for RecipeTask: records the place, then ends without doing work."""

    started: ClassVar[list[str]] = []
    log = pyqtSignal(str)
    planned = pyqtSignal()

    def __init__(self, recipe, _wkt, name, _out):
        super().__init__("fake recipe", QgsTask.Flag.CanCancel)
        RecordingRecipeTask.started.append(name)
        self.recipe = recipe
        self.place_name = name
        self.plan = None
        self.result = None

    def run(self):
        return True

    def failure(self):
        return "Testlauf"


class SimpleLoadUsesTypedPlaceTest(unittest.TestCase):
    def setUp(self):
        QgsSettings().setValue(
            "SatelliteDownloader/out_dir", tempfile.mkdtemp(prefix="sd_usability")
        )
        self.dock = SimpleDock(FakeIface(), lambda: None)
        RecordingRecipeTask.started = []
        self._recipe_task = simple_dock.RecipeTask
        self._geocode_task = simple_dock.GeocodeTask
        simple_dock.RecipeTask = RecordingRecipeTask
        self.addCleanup(self._restore)

    def _restore(self):
        simple_dock.RecipeTask = self._recipe_task
        simple_dock.GeocodeTask = self._geocode_task

    def _load(self):
        """Run load(); a pending geocode finishes and continues it in a slot."""
        self.dock.load()
        wait_until(
            lambda: self.dock.geocode_task is None and self.dock.recipe_task is None,
            10,
        )

    def test_typed_coordinates_replace_the_previous_place(self):
        self.dock.search("50.94, 6.95")
        self.dock.query.setText("-1.29, 36.82")
        self._load()
        self.assertEqual(len(RecordingRecipeTask.started), 1)
        self.assertIn("-1.29", RecordingRecipeTask.started[0])

    def test_confirmed_place_loads_without_a_new_search(self):
        self.dock.search("50.94, 6.95")
        self._load()
        self.assertIn("50.94", RecordingRecipeTask.started[0])

    def test_single_hit_continues_loading(self):
        simple_dock.GeocodeTask = fake_geocode([BONN])
        self.dock.search("50.94, 6.95")
        self.dock.query.setText("Bonn")
        self._load()
        self.assertEqual(RecordingRecipeTask.started, [BONN.name])

    def test_several_hits_wait_for_a_choice(self):
        simple_dock.GeocodeTask = fake_geocode([KOELN, BONN])
        self.dock.search("50.94, 6.95")
        self.dock.query.setText("Rhein")
        self._load()
        self.assertEqual(RecordingRecipeTask.started, [])
        self.assertEqual(self.dock.results.count(), 2)
        self._load()  # the first hit is selected; now the place is confirmed
        self.assertEqual(RecordingRecipeTask.started, [KOELN.name])

    def test_no_hit_does_not_load_the_old_place(self):
        simple_dock.GeocodeTask = fake_geocode([])
        self.dock.search("50.94, 6.95")
        self.dock.query.setText("xqzvwk")
        self._load()
        self.assertEqual(RecordingRecipeTask.started, [])

    def test_map_click_clears_the_typed_text(self):
        self.dock.query.setText("Nairobi")
        self.dock._use_canvas_extent()
        self.assertEqual(self.dock.query.text(), "")
        self._load()
        self.assertEqual(RecordingRecipeTask.started, ["Map extent"])


class ExpertBase(unittest.TestCase):
    def setUp(self):
        QgsProject.instance().clear()
        self.dlg = DownloaderDialog(FakeIface())
        self.picker = self.dlg.collection_picker
        self.addCleanup(self.dlg.close)


class EnterKeyTest(ExpertBase):
    def test_no_button_reacts_to_enter(self):
        self.dlg.show()
        self.assertEqual(
            [b.text() for b in self.dlg.findChildren(QPushButton) if b.isDefault()], []
        )

    def test_enter_in_filter_keeps_ticks(self):
        self.dlg.show()
        self.picker.setCheckedKeys(["s2-l2a", "landsat-c2-l2"])
        QTest.keyClick(self.picker.search, Qt.Key.Key_Return)
        self.assertEqual(self.picker.checkedKeys(), ["s2-l2a", "landsat-c2-l2"])

    def test_enter_in_place_field_keeps_ticks_and_sets_the_area(self):
        self.dlg.show()
        self.picker.setCheckedKeys(["s2-l2a", "landsat-c2-l2"])
        self.dlg.place_edit.setText("50.94, 6.95")
        QTest.keyClick(self.dlg.place_edit, Qt.Key.Key_Return)
        self.assertEqual(self.picker.checkedKeys(), ["s2-l2a", "landsat-c2-l2"])
        box = self.dlg._area_bbox()
        self.assertTrue(box[0] < 6.95 < box[2] and box[1] < 50.94 < box[3], box)


class TickedSelectionIsShownTest(ExpertBase):
    def test_one_tick_shows_that_collection(self):
        self.picker.setCurrentKey("s2-l2a")
        self.picker.setCheckedKeys(["landsat-c2-l2"])
        label = COLLECTIONS_BY_KEY["landsat-c2-l2"].label
        self.assertEqual(self.dlg._selection()[0].key, "landsat-c2-l2")
        self.assertIn(label, self.dlg.note_label.text())
        self.assertFalse(self.dlg.band_list.isEnabled())
        self.assertFalse(self.dlg.res_spin.isEnabled())
        self.assertIn("1 dataset", self.dlg.btn_search.text())

    def test_removing_the_last_tick_restores_the_current_collection(self):
        self.picker.setCurrentKey("s2-l2a")
        self.picker.setCheckedKeys(["landsat-c2-l2"])
        self.picker.clear_checked()
        self.assertTrue(self.dlg.band_list.isEnabled())
        self.assertTrue(self.dlg.res_spin.isEnabled())
        self.assertEqual(self.dlg.btn_search.text(), "1. Search scenes")
        self.assertIn("Processing Baseline", self.dlg.note_label.text())

    def test_ticked_current_collection_downloads_default_bands(self):
        self.picker.setCurrentKey("s2-l2a")
        for row in range(self.dlg.band_list.count()):
            self.dlg.band_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self.picker.setCheckedKeys(["s2-l2a"])
        self.assertFalse(self.dlg._use_band_choice({"s2-l2a"}))


class ResolutionTest(ExpertBase):
    def test_native_resolution_survives_the_field(self):
        for key, native in (
            ("oam:openaerialmap", 0.035),
            ("swisstopo:ch.swisstopo.swissimage-dop10", 0.1),
            ("kyfromabove:dem-phase3", 0.61),
            ("geobon:colombia-human-footprint", 309.7381974),
            ("dlr:METOP_GOME2_L3_P1D_BRO", 27800.0),
        ):
            with self.subTest(key=key):
                c = COLLECTIONS_BY_KEY[key]
                self.assertEqual(c.native_res, native)
                self.picker.setCurrentKey(key)
                self.assertEqual(self.dlg._chosen_resolution(c), native)

    def test_user_value_wins_and_zero_is_impossible(self):
        c = COLLECTIONS_BY_KEY["s2-l2a"]
        self.picker.setCurrentKey("s2-l2a")
        self.dlg.res_spin.setValue(20)
        self.assertEqual(self.dlg._chosen_resolution(c), 20)
        self.dlg.res_spin.setValue(0)
        self.assertGreater(self.dlg.res_spin.value(), 0)


class SearchLimitTest(unittest.TestCase):
    def test_limited_lists_collections_at_the_limit(self):
        outcome = selection.SearchOutcome(counts={"s2-l2a": 20, "s1-rtc": 19})
        self.assertEqual(selection.limited(outcome, 20), ["s2-l2a"])
        self.assertEqual(selection.limited(outcome, 21), [])


class SearchLimitShownTest(ExpertBase):
    def _finish(self, max_each):
        c = COLLECTIONS_BY_KEY["cop-dem-30"]  # no mask band, so no clear-sky task
        item = selection.tag({"id": "a", "properties": {}, "assets": {}}, c)
        outcome = selection.SearchOutcome(items=[item], counts={c.key: 1})
        self.dlg._search_done(
            SimpleNamespace(
                failure=lambda: None,
                isCanceled=lambda: False,
                items=outcome.items,
                collections=[c],
                outcome=outcome,
                discarded=0,
                max_items_each=max_each,
            )
        )

    def test_limit_is_reported_and_survives_new_ticks(self):
        self._finish(1)
        self.assertIn("Limit", self.dlg.log_view.toPlainText())
        self.assertIn("Limit", self.dlg.summary_label.text())
        self.dlg._check_all(False)
        self.assertIn("Limit", self.dlg.summary_label.text())

    def test_no_notice_below_the_limit(self):
        self._finish(100)
        self.assertNotIn("Limit", self.dlg.log_view.toPlainText())
        self.assertNotIn("Limit", self.dlg.summary_label.text())


class _StopDownload:
    """DownloadTask stand-in: keeps the jobs the dialog built, then stops."""

    jobs: ClassVar[list] = []

    def __init__(self, jobs):
        _StopDownload.jobs = jobs
        raise InterruptedError


class ExpertDownloadSetupTest(ExpertBase):
    """What "2. Download selected" hands to the download task."""

    def setUp(self):
        super().setUp()
        expert_dialog.DownloadTask, original = _StopDownload, expert_dialog.DownloadTask
        self.addCleanup(setattr, expert_dialog, "DownloadTask", original)
        _StopDownload.jobs = []
        self.warnings: list[tuple[str, str]] = []
        patcher = mock.patch.object(
            expert_dialog.QMessageBox,
            "warning",
            side_effect=lambda _parent, title, text: self.warnings.append(
                (title, text)
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dlg.out_dir.setFilePath(tmp.name)
        self.dlg.place_edit.setText("50.94, 6.95")
        self.dlg.search_place()  # coordinates: the area is set at once

    def show(self, *keys):
        self.dlg._set_items(
            [
                selection.tag(
                    {
                        "id": f"scene{n}",
                        "properties": {"datetime": "2026-09-01T10:00:00Z"},
                        "assets": {},
                    },
                    COLLECTIONS_BY_KEY[key],
                )
                for n, key in enumerate(keys)
            ]
        )

    def download(self) -> list:
        with contextlib.suppress(InterruptedError):
            self.dlg.start_download()
        return _StopDownload.jobs

    def tick_bands(self, *assets):
        for i in range(self.dlg.band_list.count()):
            row = self.dlg.band_list.item(i)
            row.setCheckState(
                Qt.CheckState.Checked
                if row.text() in assets
                else Qt.CheckState.Unchecked
            )

    def test_shown_dataset_uses_the_band_list_and_resolution(self):
        self.picker.setCurrentKey("s2-l2a")
        self.show("s2-l2a", "s2-l2a")
        self.tick_bands("B08", "B04")
        self.dlg.res_spin.setValue(20)
        (job,) = self.download()
        self.assertEqual(sorted(job.assets), ["B04", "B08"])
        self.assertEqual(job.resolution_m, 20.0)
        self.assertEqual(len(job.items), 2)
        self.assertIsNone(job.dst_crs_wkt)  # automatic CRS
        self.assertFalse(job.clip_to_aoi)  # a rectangle, not a polygon

    def test_ticked_datasets_use_defaults_at_native_resolution(self):
        self.picker.setCheckedKeys(["s2-l2a", "landsat-c2-l2"])
        self.show("s2-l2a", "landsat-c2-l2")
        jobs = self.download()
        self.assertEqual(
            {j.collection.key: (j.assets, j.resolution_m) for j in jobs},
            {
                c.key: (list(c.default_assets), c.native_res)
                for c in (
                    COLLECTIONS_BY_KEY["s2-l2a"],
                    COLLECTIONS_BY_KEY["landsat-c2-l2"],
                )
            },
        )

    def test_explicit_target_crs_is_passed_on(self):
        self.show("s2-l2a")
        self.dlg.cb_auto_utm.setChecked(False)
        self.dlg.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:3035"))
        (job,) = self.download()
        self.assertIn("LAEA", job.dst_crs_wkt or "")

    def test_missing_inputs_are_explained_and_nothing_starts(self):
        cases = {
            "no folder": (lambda: self.dlg.out_dir.setFilePath(""), "output folder"),
            "no bands": (lambda: self.tick_bands(), "No bands ticked"),
            "bad crs": (
                lambda: (
                    self.dlg.cb_auto_utm.setChecked(False),
                    self.dlg.crs_widget.setCrs(QgsCoordinateReferenceSystem()),
                ),
                "target CRS is not valid",
            ),
        }
        for name, (break_it, expected) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.picker.setCurrentKey("s2-l2a")
                self.show("s2-l2a")
                break_it()
                self.assertEqual(self.download(), [])
                self.assertIn(expected, self.warnings[-1][1])
                self.assertIsNone(self.dlg.download_task)

    def test_nothing_ticked_is_explained(self):
        self.show("s2-l2a")
        self.dlg._check_all(False)
        self.assertEqual(self.download(), [])
        self.assertIn("No scenes ticked", self.warnings[-1][1])


class ExpertSearchUsesTypedPlaceTest(ExpertBase):
    def test_typed_coordinates_move_the_area_before_searching(self):
        self.dlg.place_edit.setText("-1.29, 36.82")
        expert_dialog.SearchTask, original = _StopSearch, expert_dialog.SearchTask
        self.addCleanup(setattr, expert_dialog, "SearchTask", original)
        with self.assertRaises(InterruptedError):
            self.dlg.start_search()
        box = self.dlg._area_bbox()
        self.assertTrue(box[0] < 36.82 < box[2] and box[1] < -1.29 < box[3], box)

    def test_several_hits_do_not_search(self):
        expert_dialog.GeocodeTask, original = (
            fake_geocode([KOELN, BONN]),
            expert_dialog.GeocodeTask,
        )
        self.addCleanup(setattr, expert_dialog, "GeocodeTask", original)
        expert_dialog.SearchTask, search = _StopSearch, expert_dialog.SearchTask
        self.addCleanup(setattr, expert_dialog, "SearchTask", search)
        self.dlg.place_edit.setText("Rhein")
        self.dlg.start_search()
        wait_until(lambda: self.dlg.geocode_task is None, 10)
        self.assertIsNone(self.dlg.search_task)
        self.assertEqual(self.dlg.place_edit.text(), "Rhein")

    def test_drawn_rectangle_clears_the_place_text(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle

        self.dlg.place_edit.setText("Nairobi")
        self.dlg.extent_widget.setOutputExtentFromUser(
            QgsRectangle(7, 50, 8, 51), QgsCoordinateReferenceSystem("EPSG:4326")
        )
        self.assertEqual(self.dlg.place_edit.text(), "")


class DateRangeTest(ExpertBase):
    def _dates(self):
        return (self.dlg.date_from.date(), self.dlg.date_to.date())

    def test_range_returns_after_a_collection_that_ended(self):
        self.picker.setCurrentKey("s2-l2a")
        before = self._dates()
        self.picker.setCurrentKey("pc:landsat-c2-l1")
        self.assertEqual(self.dlg.date_to.date().year(), 2013)
        self.assertIn("Period", self.dlg.log_view.toPlainText())
        self.picker.setCurrentKey("s2-l2a")
        self.assertEqual(self._dates(), before)

    def test_range_set_by_the_user_is_kept(self):
        self.picker.setCurrentKey("s2-l2a")
        self.picker.setCurrentKey("pc:landsat-c2-l1")
        chosen = QDate(2012, 3, 4)
        self.dlg.date_from.setDate(chosen)
        self.picker.setCurrentKey("s2-l2a")
        self.assertEqual(self.dlg.date_from.date(), chosen)


class NoResultsHintTest(unittest.TestCase):
    def test_names_the_data_period_when_the_dates_miss_it(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        hint = selection.no_results_hint([s2], "2014-01-01", "2014-12-31", 30)
        self.assertIn("2015", hint)

    def test_suggests_the_cloud_filter_only_when_it_applies(self):
        s2 = COLLECTIONS_BY_KEY["s2-l2a"]
        s1 = COLLECTIONS_BY_KEY["s1-rtc"]
        hint = selection.no_results_hint([s2], "2025-01-01", "2025-01-02", 30)
        self.assertIn("cloud cover", hint)
        hint = selection.no_results_hint([s1], "2025-01-01", "2025-01-02", 30)
        self.assertNotIn("cloud cover", hint)


class EmptyResultShownTest(ExpertBase):
    def test_hint_is_shown_next_to_the_table(self):
        c = COLLECTIONS_BY_KEY["s2-l2a"]
        self.dlg._search_done(
            SimpleNamespace(
                failure=lambda: None,
                isCanceled=lambda: False,
                items=[],
                collections=[c],
                outcome=selection.SearchOutcome(),
                discarded=0,
                max_items_each=100,
                start="2014-01-01",
                end="2014-12-31",
                max_cloud=30.0,
            )
        )
        self.assertIn("2015", self.dlg.summary_label.text())


class ResultTableTest(ExpertBase):
    def _show(self, *keys):
        items = [
            selection.tag(
                {"id": f"scene{n}", "properties": {}, "assets": {}},
                COLLECTIONS_BY_KEY[key],
            )
            for n, key in enumerate(keys)
        ]
        self.dlg._set_items(items)

    def test_decisive_columns_come_first(self):
        self.assertLess(results_panel.COL_CLEAR, results_panel.COL_SENSOR)
        self.assertLess(results_panel.COL_COVERAGE, results_panel.COL_SENSOR)

    def test_dataset_column_only_with_several_datasets(self):
        self._show("s2-l2a", "s2-l2a")
        self.assertTrue(self.dlg.table.isColumnHidden(results_panel.COL_SENSOR))
        self._show("s2-l2a", "s1-rtc")
        self.assertFalse(self.dlg.table.isColumnHidden(results_panel.COL_SENSOR))


class PeriodTest(unittest.TestCase):
    def test_curated_collections_show_their_start(self):
        self.assertEqual(period(COLLECTIONS_BY_KEY["s2-l2a"]), "2015–today")
        self.assertEqual(period(COLLECTIONS_BY_KEY["s1-rtc"]), "2014–today")

    def test_unknown_period_is_not_guessed(self):
        c = replace(COLLECTIONS_BY_KEY["s2-l2a"], interval=(None, None))
        self.assertEqual(period(c), "n/a")
        c = replace(c, interval=(None, "2020-05-01T00:00:00Z"))
        self.assertEqual(period(c), "until 2020")


class ReadableFeedbackTest(unittest.TestCase):
    def test_no_faint_text_colours_left(self):
        root = Path(simple_dock.__file__).resolve().parent.parent
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("palette(" + "mid)", text)
                self.assertNotIn("color:" + "gray", text)

    def test_place_errors_appear_at_the_search_field(self):
        dock = SimpleDock(FakeIface(), lambda: None)
        dock._geocode_done(
            SimpleNamespace(
                failure=lambda: None,
                isCanceled=lambda: False,
                places=[],
                query="xqzvwk",
            )
        )
        self.assertIn("xqzvwk", dock.place_status.text())
        self.assertEqual(dock.status.text(), "")

    def test_expert_place_errors_appear_at_the_search_field(self):
        dlg = DownloaderDialog(FakeIface())
        dlg._geocode_done(
            SimpleNamespace(
                failure=lambda: None,
                isCanceled=lambda: False,
                places=[],
                query="xqzvwk",
            )
        )
        self.assertIn("xqzvwk", dlg.place_status.text())
        dlg.close()


class DownloadWarningTest(unittest.TestCase):
    def test_small_download_needs_no_question(self):
        self.assertIsNone(download_plan.download_warning(5, 0, 1024))

    def test_many_scenes(self):
        self.assertIn("25 scenes", download_plan.download_warning(25, 0, 1024))

    def test_raw_files_have_unknown_size(self):
        self.assertIn("a whole file", download_plan.download_warning(1, 1, 0))
        self.assertIn("whole files", download_plan.download_warning(3, 3, 0))

    def test_warning_is_not_a_question(self):
        # The dialog's title asks; its buttons name the action.
        self.assertNotIn("?", download_plan.download_warning(25, 1, 6 * 1024**3))

    def test_large_estimate(self):
        size = 6 * 1024**3
        self.assertIn("6.0 GB", download_plan.download_warning(1, 0, size))


class DockBase(unittest.TestCase):
    def setUp(self):
        QgsProject.instance().clear()
        QgsSettings().setValue(
            "SatelliteDownloader/out_dir", tempfile.mkdtemp(prefix="sd_usability")
        )
        self.iface = FakeIface()
        self.dock = SimpleDock(self.iface, lambda: None)
        self.addCleanup(self.dock.cleanup)


class MapPickTest(DockBase):
    def _click(self, x=773000, y=6607000, button=Qt.MouseButton.LeftButton):
        self.dock._on_map_click(QgsPointXY(x, y), button)

    def test_second_click_on_the_button_cancels(self):
        self.dock.btn_pick.click()
        self.assertIs(self.iface.canvas.mapTool(), self.dock.map_tool)
        self.assertTrue(self.dock.btn_pick.isChecked())
        self.dock.btn_pick.click()
        self.assertIsNot(self.iface.canvas.mapTool(), self.dock.map_tool)
        self.assertEqual(self.dock.place_status.text(), "")

    def test_picking_twice_does_not_trap_the_tool(self):
        self.dock._pick_on_map()
        self.dock._pick_on_map()
        self._click()
        self.assertIsNot(self.iface.canvas.mapTool(), self.dock.map_tool)
        self.assertFalse(self.dock.btn_pick.isChecked())

    def test_other_tool_ends_the_pick(self):
        self.dock.btn_pick.click()
        self.iface.canvas.unsetMapTool(self.dock.map_tool)
        self.assertFalse(self.dock.btn_pick.isChecked())
        self.assertEqual(self.dock.place_status.text(), "")

    def test_right_click_is_ignored(self):
        self.dock.btn_pick.click()
        self._click(button=Qt.MouseButton.RightButton)
        self.assertIsNone(self.dock.place)
        self.assertIs(self.iface.canvas.mapTool(), self.dock.map_tool)

    def test_click_outside_the_earth_keeps_the_pick_active(self):
        for x, y in ((0, 80_000_000), (90_000_000, 0)):
            with self.subTest(x=x, y=y):
                self.dock.btn_pick.setChecked(False)
                self.dock.btn_pick.click()
                self._click(x, y)
                self.assertIsNone(self.dock.place)
                self.assertIn("outside the Earth", self.dock.place_status.text())
                self.assertIs(self.iface.canvas.mapTool(), self.dock.map_tool)

    def test_click_outside_the_globe_in_degrees(self):
        self.iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.dock.btn_pick.click()
        self._click(250, 100)
        self.assertIsNone(self.dock.place)
        self.assertIn("outside the Earth", self.dock.place_status.text())


class AreaControlsTest(DockBase):
    def test_radius_only_for_circles(self):
        self.dock.set_place(BONN)  # small enough to be used as a rectangle
        self.assertFalse(self.dock.radius.isEnabled())
        self.dock.search("50.94, 6.95")
        self.assertTrue(self.dock.radius.isEnabled())

    def test_map_extent_has_no_radius_and_no_whole_area_option(self):
        big = places.Place("Spitzbergen, NO", 17, 78.5, (74, 81, 10, 35), "boundary")
        self.dock.show()
        self.dock.set_place(big)
        self.assertTrue(self.dock.cb_whole.isVisible())
        self.dock._use_canvas_extent()
        self.assertFalse(self.dock.cb_whole.isVisible())
        self.assertFalse(self.dock.radius.isEnabled())

    def test_area_outline_returns_when_the_dock_reopens(self):
        self.dock.show()
        self.dock.search("50.94, 6.95")
        self.dock.close()
        self.assertEqual(self.dock.rubber.numberOfVertices(), 0)
        self.dock.show()
        self.assertGreater(self.dock.rubber.numberOfVertices(), 0)

    def test_coordinate_name_keeps_both_values(self):
        self.dock.search("50.94, 6.95")
        self.assertIn("6.95", self.dock.area_label.text())

    def test_huge_map_extent_warns_and_names_the_resolution(self):
        canvas = self.iface.canvas
        canvas.setExtent(QgsRectangle(-20_000_000, -18_000_000, 20_000_000, 18_000_000))
        self.dock._use_canvas_extent()
        self.assertIn("Very large area", self.dock.area_label.text())
        question = self.dock._large_area_question()
        self.assertRegex(question, r"\d+ m")


class ShortNameTest(unittest.TestCase):
    def test_place_names_are_cut_at_the_comma(self):
        self.assertEqual(places.short_name("Cologne, NRW, Germany"), "Cologne")
        self.assertEqual(places.short_name("Old Town, 50667, Cologne"), "Old Town")

    def test_coordinates_stay_whole(self):
        self.assertEqual(places.short_name("50.94000, 6.95000"), "50.94000, 6.95000")
        self.assertEqual(places.short_name("-1.29, 36.82"), "-1.29, 36.82")
        self.assertEqual(
            places.short_name("Point 50.9400, 6.9500"), "Point 50.9400, 6.9500"
        )


class FolderTest(DockBase):
    def test_label_follows_the_project(self):
        before = self.dock.folder_label.toolTip()
        home = tempfile.mkdtemp(prefix="sd_project")
        QgsProject.instance().setFileName(os.path.join(home, "p.qgz"))
        self.assertNotEqual(self.dock.folder_label.toolTip(), before)
        self.assertIn("SatelliteData", self.dock.folder_label.toolTip())
        self.assertFalse(self.dock.btn_folder.isEnabled())
        QgsProject.instance().clear()
        self.assertEqual(self.dock.folder_label.toolTip(), before)
        self.assertTrue(self.dock.btn_folder.isEnabled())

    def test_folder_can_be_changed_without_a_project(self):
        target = tempfile.mkdtemp(prefix="sd_folder")
        chooser = simple_dock.QFileDialog.getExistingDirectory
        simple_dock.QFileDialog.getExistingDirectory = lambda *_a, **_k: target
        self.addCleanup(
            setattr, simple_dock.QFileDialog, "getExistingDirectory", chooser
        )
        self.dock._choose_folder()
        self.assertEqual(
            os.path.normpath(self.dock.out_root()), os.path.normpath(target)
        )
        self.assertEqual(self.dock.folder_label.toolTip(), os.path.normpath(target))

    def test_both_modes_share_the_default_folder(self):
        from satellite_downloader.gui import settings as gui_settings

        settings = QgsSettings()
        settings.remove(gui_settings.SETTINGS_PREFIX + "out_dir")
        expert = DownloaderDialog(FakeIface())
        self.addCleanup(expert.close)
        default = gui_settings.default_out_dir()
        self.assertEqual(self.dock.out_root(), default)
        self.assertEqual(expert.out_dir.filePath(), default)

    def test_cancelled_chooser_changes_nothing(self):
        before = self.dock.out_root()
        chooser = simple_dock.QFileDialog.getExistingDirectory
        simple_dock.QFileDialog.getExistingDirectory = lambda *_a, **_k: ""
        self.addCleanup(
            setattr, simple_dock.QFileDialog, "getExistingDirectory", chooser
        )
        self.dock._choose_folder()
        self.assertEqual(self.dock.out_root(), before)


class EmptyMessagesTest(DockBase):
    def test_empty_messages_leave_no_gap(self):
        self.assertTrue(self.dock.place_status.isHidden())
        self.dock.place_status.setText("Searching place …")
        self.assertFalse(self.dock.place_status.isHidden())
        self.dock.place_status.setText("")
        self.assertTrue(self.dock.place_status.isHidden())


class ResultPanelTest(DockBase):
    def test_copy_confirms(self):
        self.dock.last_attribution = "Source: Test"
        self.dock._copy_attribution()
        self.assertIn("copied", self.dock.status.text())

    def test_folder_link_survives_special_characters(self):
        href = simple_dock._folder_link("C:\\Data\\D'Arcy & #1")
        self.assertNotIn("'", href.replace("&#x27;", ""))
        self.assertIn("%23", href)


class PlainStatusTest(unittest.TestCase):
    def test_every_recipe_step_has_a_plain_name(self):
        for recipe in recipes.RECIPES:
            for step in recipe.postprocess:
                self.assertIn(step, recipes.STEP_LABELS)
        self.assertEqual(recipes.STEP_LABELS["hillshade"], "hillshade")

    def test_pipeline_details_stay_out_of_the_status_line(self):
        task = RecipeTask(recipes.RECIPES[0], "POINT(0 0)", "Place", "out")
        seen = []
        task.log.connect(seen.append)
        task._log_detail("Target CRS: EPSG:32632")
        self.assertEqual(seen, [])
        task._log("Searching acquisitions …")
        self.assertEqual(seen, ["Searching acquisitions …"])


class CoordinateFormatsTest(unittest.TestCase):
    def test_hemisphere_letters_without_degree_sign(self):
        for text in ("N50.94 E6.95", "50.94N 6.95E", "N 50,94; E 6,95", "E6.95 N50.94"):
            with self.subTest(text=text):
                lon, lat = places.parse_coordinates(text)
                self.assertAlmostEqual(lon, 6.95)
                self.assertAlmostEqual(lat, 50.94)

    def test_southern_and_western_hemispheres(self):
        self.assertEqual(places.parse_coordinates("S33.9 W151.2"), (-151.2, -33.9))

    def test_two_latitudes_are_no_coordinate(self):
        self.assertIsNone(places.parse_coordinates("N50 N6"))
        self.assertIsNone(places.parse_coordinates("Nairobi"))


class ClearSkyProgressTest(ExpertBase):
    def test_cancel_stays_available_and_progress_is_visible(self):
        c = COLLECTIONS_BY_KEY["s2-l2a"]
        items = [
            selection.tag({"id": f"s{n}", "properties": {}, "assets": {}}, c)
            for n in range(2)
        ]
        self.dlg._set_items(items)
        self.dlg.clear_task = task = SimpleNamespace(
            items=items, isCanceled=lambda: False, cancel=lambda: None
        )
        self.dlg._clear_started(len(items))
        self.assertTrue(self.dlg.btn_cancel.isEnabled())
        self.assertIn("0 of 2", self.dlg.summary_label.text())
        self.dlg._on_clear_evaluated(items[0][selection.UID_KEY], 80.0)
        self.assertIn("1 of 2", self.dlg.summary_label.text())
        self.dlg._clear_sky_done(task)
        self.assertFalse(self.dlg.btn_cancel.isEnabled())
        self.assertNotIn("of 2 scenes assessed", self.dlg.summary_label.text())


class PickerRefreshTest(ExpertBase):
    def test_no_match_says_so(self):
        self.picker.search.setText("xqzvwk")
        self.assertFalse(self.picker.empty_label.isHidden())
        self.assertIn("xqzvwk", self.picker.empty_label.text())
        self.picker.search.clear()
        self.assertTrue(self.picker.empty_label.isHidden())

    def test_expanded_groups_survive_a_refresh_and_a_filter(self):
        item = self.picker.categoryItem("Water")
        self.assertFalse(item.isExpanded())
        item.setExpanded(True)
        self.picker.refresh()
        self.assertTrue(self.picker.categoryItem("Water").isExpanded())
        self.picker.search.setText("sentinel")
        self.picker.search.clear()
        self.assertTrue(self.picker.categoryItem("Water").isExpanded())
        self.assertFalse(self.picker.categoryItem("Soil").isExpanded())

    def test_area_change_rebuilds_only_when_the_area_filter_is_on(self):
        calls = []
        original = self.picker.refresh
        self.picker.refresh = lambda *a: (calls.append(1), original(*a))
        self.dlg._on_extent_changed()
        self.assertEqual(calls, [])
        self.picker.only_here.setChecked(True)
        calls.clear()
        self.dlg._on_extent_changed()
        self.assertEqual(calls, [1])


class ClipOptionTest(ExpertBase):
    def test_clip_only_for_polygon_areas(self):
        self.assertTrue(self.dlg.rb_extent.isChecked())
        self.picker.setCurrentKey("landsat-c2-l2")
        self.assertFalse(self.dlg.cb_clip.isEnabled())


class WordingTest(unittest.TestCase):
    def test_no_sensor_wording_left_in_task_labels(self):
        c = COLLECTIONS_BY_KEY["s2-l2a"]
        task = SearchTask([c, c], {}, "2020-01-01", "2020-01-02", None, 1)
        self.assertIn("datasets", task.description())


class _StopSearch:
    def __init__(self, *_args):
        raise InterruptedError


class DockPolishTest(DockBase):
    def test_numbers_use_a_decimal_point(self):
        text = self.dock.radius.text()
        self.assertIn(".", text)
        self.assertNotIn(",", text)

    def test_place_button_is_named_like_in_expert_mode(self):
        self.assertEqual(self.dock.btn_search.text(), "Find place")

    def test_help_opens_the_documentation(self):
        with mock.patch.object(simple_dock.QDesktopServices, "openUrl") as open_url:
            self.dock.btn_help.click()
        open_url.assert_called_once_with(QUrl(stac.HELP_URL))

    def test_result_table_separates_labels_and_values(self):
        acq = SimpleNamespace(
            collection=COLLECTIONS_BY_KEY["s2-l2a"],
            when="2026-11-02T10:00:00Z",
            items=[{}],
            clear=97.0,
            coverage=100.0,
        )
        plan = SimpleNamespace(
            acquisition=acq, recipe=recipes.RECIPES[0], resolution_m=10.0, notes=[]
        )
        result = SimpleNamespace(plan=plan, attribution="Source: Test")
        self.dock._show_result(result, os.path.join(tempfile.gettempdir(), "x.tif"))
        self.assertIn("padding-right", self.dock.info.toHtml())
        self.assertIn("Clear sky in the area", self.dock.info.toPlainText())


class ExpertPolishTest(ExpertBase):
    def test_numbers_use_a_decimal_point(self):
        self.assertEqual(self.dlg.table.locale().decimalPoint(), ".")
        self.assertEqual(self.dlg.cloud_spin.locale().decimalPoint(), ".")

    def test_workflow_buttons_stand_out(self):
        for button in (self.dlg.btn_search, self.dlg.btn_download):
            with self.subTest(button.text()):
                self.assertTrue(button.font().bold())
                self.assertGreaterEqual(
                    button.minimumHeight(), expert_dialog.PRIMARY_BUTTON_HEIGHT
                )

    def test_full_name_of_the_clicked_row_is_shown(self):
        self.picker.setCurrentKey("s2-l2a")
        label = COLLECTIONS_BY_KEY["s2-l2a"].label
        self.assertIn(f"<b>{label}</b>", self.dlg.note_label.text())

    def test_dataset_column_is_named_dataset(self):
        self.assertEqual(self.picker.tree.headerItem().text(0), "Dataset")

    def test_empty_areas_explain_what_to_do(self):
        self.assertTrue(self.dlg.log_view.placeholderText())
        self.assertEqual(self.dlg.summary_label.text(), expert_dialog.START_HINT)

    def test_help_opens_the_documentation(self):
        with mock.patch.object(expert_dialog.QDesktopServices, "openUrl") as open_url:
            self.dlg.btn_help.click()
        open_url.assert_called_once_with(QUrl(stac.HELP_URL))


class ConfirmTest(unittest.TestCase):
    """Questions before slow actions name the action on their buttons."""

    def _ask(self, press: str | None):
        seen = {}

        def fake_exec(box):
            seen["buttons"] = [b.text() for b in box.buttons()]
            seen["default"] = box.defaultButton().text()
            target = next((b for b in box.buttons() if b.text() == press), None)
            box.clickedButton = lambda: target  # what the user pressed
            return 0

        with mock.patch.object(messages.QMessageBox, "exec", fake_exec):
            answer = messages.confirm(None, "Download 3 scenes?", "text", "Download")
        return answer, seen

    def test_buttons_are_named_after_the_action(self):
        answer, seen = self._ask("Download")
        self.assertTrue(answer)
        self.assertIn("Download", seen["buttons"])
        self.assertNotIn("&Yes", seen["buttons"])
        self.assertNotIn("Yes", seen["buttons"])

    def test_cancel_is_the_safe_default(self):
        answer, seen = self._ask(None)
        self.assertFalse(answer)
        self.assertNotEqual(seen["default"], "Download")


class FixHintTest(ExpertBase):
    """Error messages say how to fix the problem."""

    def test_empty_selection_names_the_checkbox(self):
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "areas", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
        layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        self.dlg.rb_layer.setChecked(True)
        self.dlg.layer_combo.setLayer(layer)
        self.dlg.cb_selected.setChecked(True)
        with self.assertRaisesRegex(ValueError, "Selected features only"):
            self.dlg._aoi()
        self.dlg.cb_selected.setChecked(False)
        self.assertTrue(self.dlg._aoi()[2])

    def test_log_hint_says_where_the_log_is(self):
        self.assertIn("Log Messages", LOG_HINT)
        self.assertIn(LOG_TAG, LOG_HINT)


if __name__ == "__main__":
    unittest.main()
