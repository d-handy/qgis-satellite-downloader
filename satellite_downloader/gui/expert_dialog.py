"""Main dialog: area, time range, collection, bands -> search -> download."""

from __future__ import annotations

import html
import json
import os

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSettings,
)
from qgis.gui import (
    QgsExtentWidget,
    QgsFileWidget,
    QgsMapLayerComboBox,
    QgsProjectionSelectionWidget,
)
from qgis.PyQt.QtCore import QDate, QLocale, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import places, selection, stac
from ..core.catalog import (
    COLLECTIONS,
    COLLECTIONS_BY_KEY,
    MODE_RAW,
    PROVIDERS,
    Collection,
)
from ..core.stac import CLEAR_KEY
from ..core.text import plural
from ..tasks import ClearSkyTask, DownloadJob, DownloadTask, GeocodeTask, SearchTask
from .collection_picker import CollectionPicker
from .download_plan import BandChoice, OutputSettings, plan_downloads
from .messages import confirm
from .results_panel import ResultsPanel, footprints_layer
from .settings import SETTINGS_PREFIX, default_out_dir

DEFAULT_COLLECTION = "s2-l2a"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
WINDOW_SIZE = (1180, 790)
LEFT_COLUMN_WIDTH = 640
PRIMARY_BUTTON_HEIGHT = 32


PLACE_RADIUS_KM = 5.0
START_HINT = "Choose datasets, an area and a period, then click 1. Search scenes."


def _checked(state: bool):
    return Qt.CheckState.Checked if state else Qt.CheckState.Unchecked


def _datasets(count: int) -> str:
    return plural(count, "dataset")


def _names(collections: list[Collection], shown: int = 4) -> str:
    names = ", ".join(c.label for c in collections[:shown])
    extra = len(collections) - shown
    return f"{names} and {extra} more" if extra > 0 else names


def _primary(button: QPushButton) -> QPushButton:
    """Main step of the workflow: bold and as tall as "Load" in simple mode."""
    font = button.font()
    font.setBold(True)
    button.setFont(font)
    button.setMinimumHeight(PRIMARY_BUTTON_HEIGHT)
    return button


def _decimals_for(value: float) -> int:
    """Fewest decimals (at most 3) that show a resolution without rounding."""
    for decimals in range(3):
        if abs(round(value, decimals) - value) < 1e-9:
            return max(decimals, 1)
    return 3


def _scrolling(widget: QWidget) -> QScrollArea:
    """The settings are taller than many screens; they scroll, the results stay."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    scroll.setMinimumWidth(
        widget.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width()
    )
    return scroll


def _found_message(task: SearchTask) -> str:
    """Log line after a search: found, discarded, unreachable."""
    found = plural(len(task.items), "scene")
    msg = f"Found {found}."
    if len(task.collections) > 1:
        found_in = len({it[selection.COLLECTION_KEY] for it in task.items})
        msg = f"Found {found} in {found_in} of {_datasets(len(task.collections))}."
    if task.discarded:
        msg += (
            f" {task.discarded} more discarded: the catalogue lists them, "
            "but their footprint does not touch the area."
        )
    if task.outcome.errors:
        unreachable = plural(len(task.outcome.errors), "catalogue")
        msg += f" {unreachable} not reachable (see above)."
    return msg


class _InputError(ValueError):
    """A setting that keeps a step from starting; shown as a warning titled ``title``."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title


class DownloaderDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.search_task: SearchTask | None = None
        self.clear_task: ClearSkyTask | None = None
        self.download_task: DownloadTask | None = None
        self.geocode_task: GeocodeTask | None = None
        self.items: list[dict] = []
        self.items_by_uid: dict[str, dict] = {}
        self.search_geometry: dict | None = None
        self._applied_place = ""  # text of the place field the area came from
        self._applying_place = False
        self._search_after_place = False
        self._search_note = ""  # stays next to the selection count
        self._fitted_dates: tuple[QDate, QDate] | None = None
        self._dates_before_fit: tuple[QDate, QDate] | None = None
        self._clear_total = 0
        self._clear_done = 0
        self.setWindowTitle("Satellite Downloader – Expert mode")
        # English text with a decimal point, whatever the Windows locale says.
        self.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self._build_ui()
        available = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(
            min(WINDOW_SIZE[0], available.width() - 40),
            min(WINDOW_SIZE[1], available.height() - 60),
        )
        self.splitter.setSizes([LEFT_COLUMN_WIDTH, self.width() - LEFT_COLUMN_WIDTH])
        self._restore_settings()
        self._on_collection_changed()
        self._on_aoi_mode_changed()
        self._update_summary()  # shows how to start while the list is empty

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        left = QWidget()
        left_layout = QVBoxLayout(left)
        for box in (
            self._source_box(),
            self._area_box(),
            self._filter_box(),
            self._band_box(),
            self._output_box(),
        ):
            left_layout.addWidget(box)
        left_layout.addStretch()
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(_scrolling(left))
        self.splitter.addWidget(self._results_side())
        self.splitter.setStretchFactor(1, 1)
        outer = QVBoxLayout(self)
        outer.addWidget(self.splitter)

        # A QDialog turns Enter in any field into a click on its default button,
        # which would be "Clear ticks" here.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _source_box(self) -> QGroupBox:
        """Dataset tree and the note on the chosen dataset."""
        src_box = QGroupBox("Data source")
        src_layout = QVBoxLayout(src_box)
        self.collection_picker = CollectionPicker(
            COLLECTIONS, PROVIDERS, self._area_bbox
        )
        self.collection_picker.currentChanged.connect(self._on_collection_changed)
        self.collection_picker.checkedChanged.connect(self._on_checked_changed)
        self.note_label = QLabel()
        self.note_label.setWordWrap(True)
        self.note_label.setTextFormat(Qt.TextFormat.RichText)
        self.note_label.setStyleSheet("font-style: italic;")
        src_layout.addWidget(self.collection_picker)
        src_layout.addWidget(self.note_label)
        return src_box

    def _area_box(self) -> QGroupBox:
        """Place search, rectangle or polygon layer."""
        aoi_box = QGroupBox("Area")
        aoi_layout = QVBoxLayout(aoi_box)
        place_row = QHBoxLayout()
        self.place_edit = QLineEdit()
        self.place_edit.setPlaceholderText("Place or coordinates, e.g. Nairobi")
        self.place_edit.returnPressed.connect(self.search_place)
        self.btn_place = QPushButton("Find place")
        self.btn_place.setToolTip(
            f"Sets the rectangle to the place (points: {PLACE_RADIUS_KM:g} km radius). "
            f"{places.OSM_ATTRIBUTION}"
        )
        self.btn_place.clicked.connect(self.search_place)
        place_row.addWidget(self.place_edit, 1)
        place_row.addWidget(self.btn_place)
        aoi_layout.addLayout(place_row)
        self.place_status = QLabel()
        self.place_status.setTextFormat(Qt.TextFormat.PlainText)  # place names
        self.place_status.setWordWrap(True)
        self.place_status.hide()
        aoi_layout.addWidget(self.place_status)
        self.rb_extent = QRadioButton(
            "Rectangle (map extent, draw on map, layer extent)"
        )
        self.rb_layer = QRadioButton("Polygon from layer")
        self.aoi_group = QButtonGroup(self)
        self.aoi_group.addButton(self.rb_extent)
        self.aoi_group.addButton(self.rb_layer)
        self.rb_extent.setChecked(True)
        self.extent_widget = QgsExtentWidget(aoi_box)
        self.extent_widget.setMapCanvas(self.iface.mapCanvas())
        self.extent_widget.setOutputCrs(QgsProject.instance().crs())
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        self.extent_widget.setOriginalExtent(canvas.extent(), canvas_crs)
        self.extent_widget.setCurrentExtent(canvas.extent(), canvas_crs)
        self.extent_widget.setOutputExtentFromCurrent()
        self.extent_widget.toggleDialogVisibility.connect(self.setVisible)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(Qgis.LayerFilter.PolygonLayer)
        # The "only my area" filter follows the chosen area.
        self.extent_widget.extentChanged.connect(self._on_extent_changed)
        self.layer_combo.layerChanged.connect(self._refresh_area_filter)
        self.cb_selected = QCheckBox("Selected features only")
        self.cb_clip = QCheckBox("Clip to polygon (otherwise its bounding rectangle)")
        self.cb_clip.setChecked(True)
        for rb in (self.rb_extent, self.rb_layer):
            rb.toggled.connect(self._on_aoi_mode_changed)
        aoi_layout.addWidget(self.rb_extent)
        aoi_layout.addWidget(self.extent_widget)
        aoi_layout.addWidget(self.rb_layer)
        aoi_layout.addWidget(self.layer_combo)
        row = QHBoxLayout()
        row.addWidget(self.cb_selected)
        row.addWidget(self.cb_clip)
        aoi_layout.addLayout(row)
        return aoi_box

    def _filter_box(self) -> QGroupBox:
        """Period, cloud limit and scene limit."""
        filt_box = QGroupBox("Period and filters")
        form = QFormLayout(filt_box)
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        dates = QHBoxLayout()
        dates.addWidget(self.date_from)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self.date_to)
        form.addRow("Period", dates)
        self.cloud_spin = QSpinBox()
        self.cloud_spin.setRange(0, 100)
        self.cloud_spin.setValue(30)
        self.cloud_spin.setSuffix(" %")
        form.addRow("Max. cloud cover", self.cloud_spin)
        self.max_items_spin = QSpinBox()
        self.max_items_spin.setRange(1, 2000)
        self.max_items_spin.setValue(100)
        form.addRow("Max. scenes per dataset", self.max_items_spin)
        return filt_box

    def _band_box(self) -> QGroupBox:
        """Bands of the shown dataset, in output order."""
        band_box = QGroupBox("Bands")
        band_layout = QVBoxLayout(band_box)
        self.band_list = QListWidget()
        self.band_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.band_list.setToolTip("Drag and drop to set the band order of the result")
        self.band_list.setMaximumHeight(150)
        band_layout.addWidget(self.band_list)
        return band_box

    def _output_box(self) -> QGroupBox:
        """Resolution, resampling, CRS, mosaic and folder."""
        out_box = QGroupBox("Output")
        out_form = QFormLayout(out_box)
        self.res_spin = QDoubleSpinBox()
        self.res_spin.setDecimals(3)
        self.res_spin.setRange(0.001, 100000)
        self.res_spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        self.res_spin.setSuffix(" m")
        out_form.addRow("Resolution", self.res_spin)
        self.resampling_combo = QComboBox()
        for alg in ("nearest", "bilinear", "cubic", "average"):
            self.resampling_combo.addItem(alg)
        self.resampling_combo.setToolTip(
            "Applies to measured values. Class and quality bands (land cover, "
            "SCL, QA) always use nearest neighbour, or the majority class when "
            "coarser than native."
        )
        out_form.addRow("Resampling", self.resampling_combo)
        self.cb_auto_utm = QCheckBox(
            "Automatic (CRS of the scenes, otherwise UTM zone)"
        )
        self.cb_auto_utm.setToolTip(
            "Uses the CRS most scenes are in, so their pixels stay unchanged. "
            "Geographic sources (S1 GRD, DEM) go to the UTM zone of the area."
        )
        self.cb_auto_utm.setChecked(True)
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setCrs(QgsProject.instance().crs())
        self.cb_auto_utm.toggled.connect(lambda on: self.crs_widget.setEnabled(not on))
        self.crs_widget.setEnabled(False)
        out_form.addRow("Target CRS", self.cb_auto_utm)
        out_form.addRow("", self.crs_widget)
        self.cb_mosaic = QCheckBox("Combine scenes of the same day into one mosaic")
        self.cb_mosaic.setChecked(True)
        out_form.addRow(self.cb_mosaic)
        self.cb_load = QCheckBox("Load results into QGIS")
        self.cb_load.setChecked(True)
        out_form.addRow(self.cb_load)
        self.out_dir = QgsFileWidget()
        self.out_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        out_form.addRow("Folder", self.out_dir)
        return out_box

    def _results_side(self) -> QWidget:
        """Search button, result list, download controls and log."""
        right = QWidget()
        right_layout = QVBoxLayout(right)
        search_row = QHBoxLayout()
        self.btn_search = _primary(QPushButton("1. Search scenes"))
        self.btn_search.clicked.connect(self.start_search)
        self.cb_footprints = QCheckBox("Show footprints as a layer")
        search_row.addWidget(self.btn_search)
        search_row.addWidget(self.cb_footprints)
        search_row.addStretch()
        btn_help = QPushButton("Help")
        btn_help.setToolTip("Open the documentation in the web browser.")
        btn_help.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(stac.HELP_URL)))
        self.btn_help = btn_help
        search_row.addWidget(btn_help)
        right_layout.addLayout(search_row)

        self.results = ResultsPanel()
        self.results.ticksChanged.connect(self._update_summary)
        # The dialog's own names for the panel's parts, used throughout.
        self.table = self.results.table
        self.summary_label = self.results.summary_label
        right_layout.addWidget(self.results, 3)

        dl_row = QHBoxLayout()
        self.btn_download = _primary(QPushButton("2. Download selected"))
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.start_download)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_tasks)
        self.progress = QProgressBar()
        dl_row.addWidget(self.btn_download)
        dl_row.addWidget(self.btn_cancel)
        dl_row.addWidget(self.progress, 1)
        right_layout.addLayout(dl_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Search and download messages appear here.")
        right_layout.addWidget(self.log_view, 2)
        return right

    # ------------------------------------------------------------ settings

    def _restore_settings(self):
        s = QgsSettings()
        key = s.value(SETTINGS_PREFIX + "collection", DEFAULT_COLLECTION)
        self.collection_picker.setCurrentKey(
            key if key in COLLECTIONS_BY_KEY else DEFAULT_COLLECTION
        )
        self.out_dir.setFilePath(
            s.value(SETTINGS_PREFIX + "out_dir", default_out_dir())
        )
        self.cloud_spin.setValue(int(s.value(SETTINGS_PREFIX + "cloud", 30)))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS_PREFIX + "collection", self.collection.key)
        s.setValue(SETTINGS_PREFIX + "out_dir", self.out_dir.filePath())
        s.setValue(SETTINGS_PREFIX + "cloud", self.cloud_spin.value())

    # ------------------------------------------------------------ helpers

    @property
    def collection(self) -> Collection:
        return COLLECTIONS_BY_KEY[
            self.collection_picker.currentKey() or DEFAULT_COLLECTION
        ]

    def log(self, msg: str):
        self.log_view.appendPlainText(msg)

    def _area_bbox(self) -> tuple[float, float, float, float] | None:
        """AOI bounding box in WGS84 for the "only my area" filter, None if unset."""
        try:
            geom, crs, _ = self._aoi()
        except (ValueError, AttributeError):  # no AOI yet, or widgets not built
            return None
        box = self._transformed(geom, crs, WGS84).boundingBox()
        return (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())

    def _on_collection_changed(self, *_):
        c = self.collection
        # The list elides long names; the full one is shown here.
        details = [f"<b>{html.escape(c.label)}</b>"]
        if c.note:
            details.append(html.escape(c.note))
        if c.license:
            details.append(html.escape(f"Licence: {c.license}"))
        self.note_label.setText(" · ".join(details))
        self.note_label.setVisible(True)
        self._fit_dates(c)
        self.band_list.clear()
        ordered = list(c.default_assets) + [
            a for a in c.assets if a not in c.default_assets
        ]
        for asset in ordered:
            it = QListWidgetItem(asset)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(_checked(asset in c.default_assets))
            self.band_list.addItem(it)
        self._show_resolution(c.native_res)
        self.cloud_spin.setEnabled(c.has_cloud_cover)
        self.date_from.setEnabled(c.time_filter)
        self.date_to.setEnabled(c.time_filter)
        cog = c.mode != MODE_RAW
        for w in (
            self.res_spin,
            self.resampling_combo,
            self.cb_auto_utm,
            self.cb_mosaic,
            self.cb_clip,
            self.cb_load,
        ):
            w.setEnabled(cog)
        self.crs_widget.setEnabled(cog and not self.cb_auto_utm.isChecked())
        self._on_aoi_mode_changed()
        self._update_mode()

    def _show_resolution(self, value: float) -> None:
        decimals = _decimals_for(value)
        self.res_spin.setDecimals(decimals)
        # setDecimals rounds the limits; the minimum must stay above zero.
        self.res_spin.setRange(10.0**-decimals, 100000)
        self.res_spin.setValue(value)

    def _chosen_resolution(self, c: Collection) -> float:
        """The field's value; the exact native one if the field only rounds it."""
        value = self.res_spin.value()
        if abs(value - round(c.native_res, self.res_spin.decimals())) < 1e-9:
            return c.native_res
        return value

    def _dates(self) -> tuple[QDate, QDate]:
        return (self.date_from.date(), self.date_to.date())

    def _set_dates(self, dates: tuple[QDate, QDate]) -> None:
        self.date_from.setDate(dates[0])
        self.date_to.setDate(dates[1])

    def _fit_dates(self, c: Collection) -> None:
        """Move the range into the time span of a collection that ended, and back.

        The previous range returns with the next running collection, unless the
        user changed the moved range in between.
        """
        if not c.time_filter:
            return
        end = QDate.fromString((c.interval[1] or "")[:10], "yyyy-MM-dd")
        if end.isValid() and end < QDate.currentDate().addDays(-60):
            if self._dates() != self._fitted_dates:
                self._dates_before_fit = self._dates()
            start = QDate.fromString((c.interval[0] or "")[:10], "yyyy-MM-dd")
            begin = end.addMonths(-12)
            self._set_dates((max(begin, start) if start.isValid() else begin, end))
            self._fitted_dates = self._dates()
            self.log(
                f"Period adjusted to {c.label}: "
                f"{self.date_from.text()} to {self.date_to.text()}."
            )
            return
        if self._dates_before_fit is not None and self._dates() == self._fitted_dates:
            self._set_dates(self._dates_before_fit)
            self.log(
                f"Period restored: {self.date_from.text()} to {self.date_to.text()}."
            )
        self._fitted_dates = None

    def _on_aoi_mode_changed(self, *_):
        layer_mode = self.rb_layer.isChecked()
        self.extent_widget.setEnabled(not layer_mode)
        self.layer_combo.setEnabled(layer_mode)
        self.cb_selected.setEnabled(layer_mode)
        self.cb_clip.setEnabled(layer_mode and self.collection.mode != MODE_RAW)

    def _selected_assets(self) -> list[str]:
        return [
            self.band_list.item(i).text()
            for i in range(self.band_list.count())
            if self.band_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _aoi(self) -> tuple[QgsGeometry, QgsCoordinateReferenceSystem, bool]:
        """Return (geometry, crs, is_polygon_aoi) or raise ValueError."""
        return self._extent_aoi() if self.rb_extent.isChecked() else self._layer_aoi()

    def _extent_aoi(self) -> tuple[QgsGeometry, QgsCoordinateReferenceSystem, bool]:
        if not self.extent_widget.isValid():
            raise ValueError(
                "No area chosen. Draw a rectangle on the map, use the map "
                "extent, or find a place."
            )
        rect = self.extent_widget.outputExtent()
        if rect.isEmpty():
            raise ValueError("The rectangle has no size. Draw a larger one.")
        return QgsGeometry.fromRect(rect), self.extent_widget.outputCrs(), False

    def _layer_aoi(self) -> tuple[QgsGeometry, QgsCoordinateReferenceSystem, bool]:
        layer = self.layer_combo.currentLayer()
        if layer is None:
            raise ValueError('Choose a polygon layer, or switch to "Rectangle".')
        selected_only = self.cb_selected.isChecked()
        feats = layer.selectedFeatures() if selected_only else layer.getFeatures()
        geoms = [f.geometry() for f in feats if f.hasGeometry()]
        if not geoms and selected_only:
            raise ValueError(
                "No features are selected in this layer. Select some, or "
                'untick "Selected features only".'
            )
        if not geoms:
            raise ValueError("This layer has no geometries.")
        geom = QgsGeometry.unaryUnion(geoms)
        if geom.isEmpty():
            raise ValueError(
                "The polygons do not form a valid area. Fix the geometries "
                "(Vector → Geometry Tools → Fix Geometries) and try again."
            )
        return geom.makeValid(), layer.crs(), True

    @staticmethod
    def _transformed(geom: QgsGeometry, src, dst) -> QgsGeometry:
        g = QgsGeometry(geom)
        if src != dst:
            g.transform(QgsCoordinateTransform(src, dst, QgsProject.instance()))
        return g

    @staticmethod
    def _utm_for(geom_wgs84: QgsGeometry) -> QgsCoordinateReferenceSystem:
        pt = geom_wgs84.centroid().asPoint()
        zone = min(60, max(1, int((pt.x() + 180) // 6) + 1))
        epsg = (32600 if pt.y() >= 0 else 32700) + zone
        return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")

    # ------------------------------------------------------------ selection

    def _selection(self) -> list[Collection]:
        """Ticked collections, or the current one when nothing is ticked."""
        keys = self.collection_picker.checkedKeys()
        if keys:
            return [COLLECTIONS_BY_KEY[k] for k in keys if k in COLLECTIONS_BY_KEY]
        return [self.collection]

    def _on_checked_changed(self, keys: list) -> None:
        if keys:
            self._update_mode()
        else:  # no ticks left: restore note, bands and filters of the clicked row
            self._on_collection_changed()

    def _use_band_choice(self, keys: set[str]) -> bool:
        """Band list and resolution apply only to the clicked row without ticks."""
        return not self.collection_picker.checkedKeys() and keys == {
            self.collection.key
        }

    def _update_mode(self) -> None:
        """Ticks win over the clicked row: default bands and native resolution."""
        ticked = bool(self.collection_picker.checkedKeys())
        chosen = self._selection()
        self.band_list.setEnabled(not ticked)
        self.res_spin.setEnabled(not ticked and self.collection.mode != MODE_RAW)
        if not ticked:
            self.btn_search.setText("1. Search scenes")
            return
        self.note_label.setText(
            html.escape(
                f"Ticked: {_names(chosen)}. "
                "Downloads use the default bands at native resolution."
            )
        )
        self.note_label.setVisible(True)
        self.cloud_spin.setEnabled(any(c.has_cloud_cover for c in chosen))
        timed = any(c.time_filter for c in chosen)
        self.date_from.setEnabled(timed)
        self.date_to.setEnabled(timed)
        cog = any(c.mode != MODE_RAW for c in chosen)
        for widget in (
            self.resampling_combo,
            self.cb_auto_utm,
            self.cb_mosaic,
            self.cb_load,
        ):
            widget.setEnabled(cog)
        self.crs_widget.setEnabled(cog and not self.cb_auto_utm.isChecked())
        self.btn_search.setText(f"1. Search {_datasets(len(chosen))}")

    # ------------------------------------------------------------ place

    def search_place(self):
        query = self.place_edit.text().strip()
        if not query or self.geocode_task is not None:
            return
        coords = places.parse_coordinates(query)
        if coords:
            lon, lat = coords
            self._apply_place(places.Place(query, lon, lat, None, "coordinate"))
            return
        task = GeocodeTask(query)
        task.taskCompleted.connect(lambda: self._geocode_done(task))
        task.taskTerminated.connect(lambda: self._geocode_done(task))
        self.geocode_task = task
        self.btn_place.setEnabled(False)
        self._refresh_cancel()
        QgsApplication.taskManager().addTask(task)

    def _geocode_done(self, task: GeocodeTask):
        self.geocode_task = None
        self.btn_place.setEnabled(True)
        search_next, self._search_after_place = self._search_after_place, False
        self._refresh_cancel()
        if task.isCanceled():
            self._show_place_status("Place search cancelled.")
            return
        if task.failure():
            self._show_place_status(f"Place search failed: {task.failure()}")
            return
        if not task.places:
            self._show_place_status(
                f'No place "{task.query}" found. Try another spelling or '
                "enter coordinates."
            )
            return
        if len(task.places) == 1:
            self._apply_place(task.places[0])
            if search_next:
                self.start_search()
            return
        self._show_place_status(
            "Several places found: pick one from the list, then click "
            '"1. Search scenes".'
        )
        menu = QMenu(self)
        for place in task.places:
            action = menu.addAction(place.name)
            action.triggered.connect(lambda _=False, p=place: self._apply_place(p))
        menu.popup(self.btn_place.mapToGlobal(self.btn_place.rect().bottomLeft()))

    def _show_place_status(self, message: str) -> None:
        self.place_status.setText(message)
        self.place_status.setVisible(bool(message))
        if message:
            self.log(message)

    def _apply_place(self, place: places.Place):
        self._show_place_status("")
        area = places.area_for(place, PLACE_RADIUS_KM)
        box = QgsGeometry.fromWkt(area.wkt).boundingBox()
        self.rb_extent.setChecked(True)
        self._applied_place = self.place_edit.text().strip()
        self._applying_place = True
        try:
            self.extent_widget.setOutputExtentFromUser(QgsRectangle(box), WGS84)
        finally:
            self._applying_place = False
        canvas = self.iface.mapCanvas()
        shown = self._transformed(
            QgsGeometry.fromRect(box), WGS84, canvas.mapSettings().destinationCrs()
        ).boundingBox()
        shown.scale(1.3)
        canvas.setExtent(shown)
        canvas.refresh()
        self.log(
            f"Area: {places.short_name(place.name)} "
            f"({area.width_km:.1f} × {area.height_km:.1f} km)"
            + (f" – {area.note}" if area.note else "")
        )

    def _on_extent_changed(self, *_):
        if not self._applying_place:  # a rectangle of its own replaces the place
            self.place_edit.clear()
            self._applied_place = ""
        self._refresh_area_filter()

    def _refresh_area_filter(self, *_):
        if self.collection_picker.only_here.isChecked():
            self.collection_picker.refresh()

    def _place_is_new(self) -> bool:
        text = self.place_edit.text().strip()
        return bool(text) and text != self._applied_place

    # ------------------------------------------------------------ search

    def start_search(self):
        if self._busy() or self._waiting_for_place():
            return
        try:
            wgs, start, end = self._search_area_and_period()
        except _InputError as exc:
            QMessageBox.warning(self, exc.title, str(exc))
            return
        todo = self._searchable(self._selection(), wgs, start, end)
        if todo:
            self._launch_search(todo, wgs, start, end)

    def _waiting_for_place(self) -> bool:
        """Find a typed but unconfirmed place first, so the search does not use
        the old area. True if the search continues in ``_geocode_done``."""
        if not self.rb_extent.isChecked():
            return False
        if self.geocode_task is None and not self._place_is_new():
            return False
        self._search_after_place = True
        if self.geocode_task is None:
            self.search_place()
        if self.geocode_task is not None:
            return True
        self._search_after_place = False
        return False

    def _search_area_and_period(self) -> tuple[QgsGeometry, str, str]:
        """(AOI in WGS 84, start, end); raises _InputError for a missing input."""
        try:
            geom, crs, _ = self._aoi()
        except ValueError as exc:
            raise _InputError("Area", str(exc)) from exc
        if self.date_from.date() > self.date_to.date():
            raise _InputError(
                "Period",
                "The start date is after the end date. Choose an earlier start date.",
            )
        start = self.date_from.date().toString("yyyy-MM-dd")
        end = self.date_to.date().toString("yyyy-MM-dd")
        return self._transformed(geom, crs, WGS84), start, end

    def _searchable(
        self, chosen: list[Collection], wgs: QgsGeometry, start: str, end: str
    ) -> list[Collection]:
        """Datasets whose extent and period match; explains the ones left out."""
        if len(chosen) == 1:
            return chosen
        box = wgs.boundingBox()
        area = (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
        todo, skipped = selection.prefilter(chosen, area, start, end)
        if skipped:
            self.log(
                f"{_datasets(len(skipped))} skipped because area or period "
                "do not match: "
                + ", ".join(c.label for c, _ in skipped[:6])
                + (" …" if len(skipped) > 6 else "")
            )
        if not todo:
            QMessageBox.information(
                self,
                "No matching datasets",
                "None of the chosen datasets covers this area in the chosen period.",
            )
        return todo

    def _launch_search(
        self, todo: list[Collection], wgs: QgsGeometry, start: str, end: str
    ) -> None:
        self._search_note = ""
        self.search_geometry = json.loads(wgs.asJson(6))
        task = SearchTask(
            todo,
            self.search_geometry,
            start,
            end,
            float(self.cloud_spin.value()),
            self.max_items_spin.value(),
        )
        task.log.connect(self.log)
        task.progressChanged.connect(lambda p: self.progress.setValue(int(p)))
        task.taskCompleted.connect(lambda: self._search_done(task))
        task.taskTerminated.connect(lambda: self._search_done(task))
        self.search_task = task
        self._set_running(True)
        self.progress.setRange(0, 100 if len(todo) > 1 else 0)
        what = todo[0].label if len(todo) == 1 else _datasets(len(todo))
        self.log(f"Searching {what}, {start} to {end} …")
        QgsApplication.taskManager().addTask(task)

    def _search_done(self, task: SearchTask):
        self.search_task = None
        self._set_running(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        if task.failure():
            self.log(f"Search failed: {task.failure()}")
            QMessageBox.critical(self, "Search failed", task.failure())
            return
        if task.isCanceled():
            self.log("Search cancelled.")
            return
        self.log(_found_message(task))
        self._search_note = self._search_note_for(task)
        self._set_items(task.items)
        if self.cb_footprints.isChecked() and task.items:
            self._add_footprints(task.items)
        if any(selection.item_collection(it).mask is not None for it in task.items):
            self._start_clear_sky(task.items)

    def _search_note_for(self, task: SearchTask) -> str:
        """Note next to the selection count: limit reached, or what to try."""
        at_limit = selection.limited(task.outcome, task.max_items_each)
        note = ""
        if at_limit:
            note = (
                f"Limit of {task.max_items_each} per dataset reached: there may "
                "be more scenes."
            )
            names = _names([COLLECTIONS_BY_KEY[k] for k in at_limit], 6)
            self.log(f"{note} Affected: {names}.")
        if not task.items and not task.outcome.errors:
            note = selection.no_results_hint(
                task.collections, task.start, task.end, task.max_cloud
            )
        return note

    def _start_clear_sky(self, items: list[dict]):
        task = ClearSkyTask(items, self.search_geometry or {})
        task.evaluated.connect(self._on_clear_evaluated)
        task.taskCompleted.connect(lambda: self._clear_sky_done(task))
        task.taskTerminated.connect(lambda: self._clear_sky_done(task))
        task.progressChanged.connect(self._on_clear_progress)
        self.clear_task = task
        self.log("Computing the cloud-free share in the area …")
        self._clear_started(len(task.items))
        QgsApplication.taskManager().addTask(task)

    def _clear_started(self, total: int) -> None:
        self._clear_total = total
        self._clear_done = 0
        self._refresh_cancel()
        self._update_summary()

    def _on_clear_progress(self, value: float) -> None:
        if not self._busy():
            self.progress.setValue(int(value))

    def _on_clear_evaluated(self, uid: str, value: float):
        self._clear_done += 1
        self._update_summary()
        self.results.set_clear(uid, value)

    def _clear_sky_done(self, task: ClearSkyTask):
        if self.clear_task is task:
            self.clear_task = None
            self._clear_total = 0
            if not self._busy():
                self.progress.setValue(0)
            self._refresh_cancel()
            self._update_summary()
        if task.isCanceled():
            self.log("Cloud assessment cancelled.")
        else:
            known = [it for it in task.items if it.get(CLEAR_KEY) is not None]
            self.log(
                f"Cloud assessment done ({len(known)} of {len(task.items)} scenes)."
            )

    def _set_items(self, items: list[dict]):
        if self.clear_task is not None:
            self.clear_task.cancel()
            self.clear_task = None
            self._clear_total = 0
        self.items = items
        self.items_by_uid = {it[selection.UID_KEY]: it for it in items}
        self.results.set_items(items)  # reports the ticks: _update_summary

    def _check_all(self, state: bool):
        self.results.check_all(state)

    def _checked_items(self) -> list[dict]:
        return [self.items_by_uid[uid] for uid in self.results.checked_uids()]

    def _update_summary(self):
        n = len(self._checked_items())
        parts = [f"{n} of {len(self.items)} selected."] if self.items else []
        if self._clear_total:
            parts.append(
                f"Clouds: {self._clear_done} of {self._clear_total} scenes assessed."
            )
        if self._search_note:
            parts.append(self._search_note)
        if not parts:
            parts.append(START_HINT)
        self.summary_label.setText(" ".join(parts))
        self.btn_download.setEnabled(n > 0 and not self._busy())

    def _add_footprints(self, items: list[dict]):
        QgsProject.instance().addMapLayer(footprints_layer(items))

    # ------------------------------------------------------------ download

    def start_download(self):
        if self._busy():
            return
        items = self._checked_items()
        keys = {it[selection.COLLECTION_KEY] for it in items}
        # Free band and resolution choice only for the collection shown in the
        # band list; everything else uses its defaults at native resolution.
        single = self._use_band_choice(keys)
        try:
            if not items:
                raise _InputError(
                    "Download", "No scenes ticked. Tick at least one scene in the list."
                )
            settings = self._output_settings()
            choice = (
                BandChoice(
                    self._selected_assets(), self._chosen_resolution(self.collection)
                )
                if single
                else None
            )
            plan = plan_downloads(items, settings, choice)
        except _InputError as exc:
            QMessageBox.warning(self, exc.title, str(exc))
            return
        except ValueError as exc:  # no bands ticked
            QMessageBox.warning(self, "Download", str(exc))
            return
        warning = plan.warning(len(items))
        title = f"Download {plural(len(items), 'scene')}?"
        if warning and not confirm(self, title, warning, "Download"):
            return
        self._save_settings()
        self._launch_download(plan.jobs, len(items), settings.out_dir, single)

    def _output_settings(self) -> OutputSettings:
        """Folder, area and output options; raises _InputError if one is missing."""
        out_dir = self.out_dir.filePath().strip()
        if not out_dir:
            raise _InputError(
                "Download", 'Choose an output folder under "Output → Folder".'
            )
        try:
            geom, crs, is_polygon = self._aoi()
        except ValueError as exc:
            raise _InputError("Area", str(exc)) from exc
        # Densify so straight edges in the layer CRS stay straight in lon/lat.
        extent = geom.boundingBox()
        step = max(extent.width(), extent.height()) / 50 or 1
        wgs = self._transformed(geom.densifyByDistance(step), crs, WGS84)
        bbox = self._transformed(wgs, WGS84, self._utm_for(wgs)).boundingBox()
        return OutputSettings(
            out_dir=out_dir,
            aoi_wgs84_wkt=wgs.asWkt(),
            area_size_m=(bbox.width(), bbox.height()),
            clip_to_aoi=is_polygon and self.cb_clip.isChecked(),
            mosaic_by_date=self.cb_mosaic.isChecked(),
            dst_crs_wkt=self._target_crs_wkt(),
            resampling=self.resampling_combo.currentText(),
        )

    def _target_crs_wkt(self) -> str | None:
        """None for the automatic choice; raises _InputError for an invalid CRS."""
        if self.cb_auto_utm.isChecked():
            return None
        dst_crs = self.crs_widget.crs()
        if not dst_crs.isValid():
            raise _InputError(
                "CRS",
                'The target CRS is not valid. Choose another one, or tick "Automatic".',
            )
        return dst_crs.toWkt(QgsCoordinateReferenceSystem.WktVariant.WKT_PREFERRED_GDAL)

    def _launch_download(
        self, jobs: list[DownloadJob], scenes: int, out_dir: str, single: bool
    ) -> None:
        task = DownloadTask(jobs)
        task.log.connect(self.log)
        task.progressChanged.connect(lambda p: self.progress.setValue(int(p)))
        task.taskCompleted.connect(lambda: self._download_done(task))
        task.taskTerminated.connect(lambda: self._download_done(task))
        self.download_task = task
        self._set_running(True)
        self.progress.setValue(0)
        bands = "" if single else ", default bands at native resolution"
        self.log(
            f"Starting download of {plural(scenes, 'scene')} from "
            f"{_datasets(len(jobs))} to {out_dir}{bands}"
        )
        QgsApplication.taskManager().addTask(task)

    def _download_done(self, task: DownloadTask):
        self.download_task = None
        self._set_running(False)
        if task.isCanceled():
            self.log("Download cancelled.")
        elif task.failure():
            self.log(f"Download failed: {task.failure()}")
            self.iface.messageBar().pushMessage(
                "Satellite Downloader", task.failure(), Qgis.MessageLevel.Critical
            )
        else:
            msg = (
                f"{plural(len(task.outputs), 'file')} from {_datasets(len(task.jobs))}"
            )
            if task.empty:
                msg += f", {task.empty} without valid pixels skipped"
            if task.failures:
                msg += f", {plural(task.failures, 'error')} (see the log)"
            self.log(f"Done: {msg}")
            level = (
                Qgis.MessageLevel.Warning
                if task.failures
                else Qgis.MessageLevel.Success
            )
            self.iface.messageBar().pushMessage("Satellite Downloader", msg, level)
        if self.cb_load.isChecked():
            for job, result in task.results:
                if result.outputs and job.collection.mode != MODE_RAW:
                    self._load_outputs(result.outputs, job.collection)

    def _load_outputs(self, paths: list[str], collection: Collection):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(collection.label) or root.insertGroup(
            0, collection.label
        )
        for path in paths:
            layer = QgsRasterLayer(path, os.path.splitext(os.path.basename(path))[0])
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)

    # ------------------------------------------------------------ state

    def _busy(self) -> bool:
        return self.search_task is not None or self.download_task is not None

    def _set_running(self, running: bool):
        self.btn_search.setEnabled(not running)
        self.collection_picker.setEnabled(not running)
        self._refresh_cancel()
        self._update_summary()

    def _refresh_cancel(self) -> None:
        self.btn_cancel.setEnabled(
            any(
                task is not None
                for task in (
                    self.search_task,
                    self.clear_task,
                    self.download_task,
                    self.geocode_task,
                )
            )
        )

    def cancel_tasks(self):
        for task in (
            self.search_task,
            self.clear_task,
            self.download_task,
            self.geocode_task,
        ):
            if task is not None:
                task.cancel()
