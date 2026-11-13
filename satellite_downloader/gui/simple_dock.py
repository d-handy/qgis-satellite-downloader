"""Simple mode: place → purpose → load."""

from __future__ import annotations

import html
import os
import tempfile
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
)
from qgis.gui import QgsDockWidget, QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QLocale, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core import places, recipes, stac
from ..core.catalog import COLLECTIONS_BY_KEY
from ..tasks import GeocodeTask, PreviewTask, RecipeTask
from . import styling
from .messages import confirm

SETTINGS_PREFIX = "SatelliteDownloader/"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
LARGE_AREA_KM = 100.0
MERCATOR_MAX_LAT = 85.06  # web maps end here; beyond it clicks run towards the pole
HINT_FONT_SCALE = 0.9
FOLDER_LABEL_CHARS = 34  # in average characters; a long path must not widen the dock


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    font = label.font()
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * HINT_FONT_SCALE)
        label.setFont(font)
    return label


class _Message(QLabel):
    """Feedback the user has to read: normal text colour, no gap while empty.

    Plain text by default: messages carry place names and server replies,
    which must not be interpreted as HTML.
    """

    def __init__(self, rich: bool = False):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.hide()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


def _heading(text: str) -> QLabel:
    label = QLabel(f"<b>{html.escape(text)}</b>")
    label.setContentsMargins(0, 8, 0, 0)
    return label


def _folder_link(folder: str) -> str:
    """Escaped href, whatever characters a folder name contains."""
    return html.escape(
        QUrl.fromLocalFile(folder).toString(QUrl.FullyEncoded), quote=True
    )


def _add_terrain_layers(group, dem_path: str, derived: dict[str, str]) -> None:
    """Hillshade over coloured elevation, slope hidden below."""
    shade = styling.add_layer(group, derived["hillshade"], "Hillshade")
    if shade:
        styling.style_hillshade_overlay(shade)
    dem = styling.add_layer(group, dem_path, "Elevation (m)")
    if dem:
        styling.style_ramp(dem, "Spectral", invert=True)
    slope = styling.add_layer(group, derived["slope"], "Slope (°)", visible=False)
    if slope:
        styling.style_ramp(slope, "Reds")


def _add_image_layer(group, path: str, style: str, acq) -> None:
    label = "Backscatter (dB)" if style == "sar" else acq.collection.label
    layer = styling.add_layer(group, path, label)
    if not layer:
        return
    if style == "rgb":
        styling.style_rgb(layer)
    else:
        styling.style_gray(layer)
    styling.set_acquisition_time(layer, acq.when)


class SimpleDock(QgsDockWidget):
    def __init__(self, iface, open_expert):
        super().__init__("Satellite data")
        # English text with a decimal point, whatever the Windows locale says.
        self.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self.setObjectName("SatelliteDownloaderSimpleDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.open_expert = open_expert
        self.place: places.Place | None = None
        self.area: places.Area | None = None
        self.geocode_task: GeocodeTask | None = None
        self.recipe_task: RecipeTask | None = None
        self.preview_task: PreviewTask | None = None
        self.preview_layer_id: str | None = None
        self.last_attribution = ""
        self._applied_query = ""  # text of the search field the place came from
        self._load_after_search = False

        self.rubber = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
        self.rubber.setColor(QColor(255, 140, 0, 60))
        self.rubber.setStrokeColor(QColor(255, 140, 0))
        self.rubber.setWidth(2)
        self.map_tool = QgsMapToolEmitPoint(self.canvas)
        self.map_tool.canvasClicked.connect(self._on_map_click)
        self.map_tool.deactivated.connect(self._on_pick_ended)
        self._previous_tool = None

        self._build_ui()
        project = QgsProject.instance()
        project.homePathChanged.connect(self._update_folder_label)
        project.readProject.connect(self._update_folder_label)
        project.cleared.connect(self._update_folder_label)

    # -- UI -----------------------------------------------------------------------

    def _build_ui(self):
        body = QWidget()
        layout = QVBoxLayout(body)

        layout.addWidget(_heading("1. Place"))
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(
            "Place or coordinates, e.g. Atacama or 50.94, 6.95"
        )
        self.query.returnPressed.connect(self.search)
        self.btn_search = QPushButton("Find place")
        self.btn_search.clicked.connect(self.search)
        row.addWidget(self.query, 1)
        row.addWidget(self.btn_search)
        layout.addLayout(row)

        self.results = QListWidget()
        self.results.setMaximumHeight(110)
        self.results.hide()
        self.results.currentItemChanged.connect(self._on_result_selected)
        layout.addWidget(self.results)
        self.place_status = _Message()
        layout.addWidget(self.place_status)

        row = QHBoxLayout()
        self.btn_pick = QPushButton("Click on map")
        self.btn_pick.setCheckable(True)
        self.btn_pick.setToolTip(
            "Click a point on the map; the area is a circle with the radius "
            "set below. Press again to cancel."
        )
        self.btn_pick.clicked.connect(self._pick_on_map)
        btn_extent = QPushButton("Map extent")
        btn_extent.setToolTip("Use the visible map rectangle as the area.")
        btn_extent.clicked.connect(self._use_canvas_extent)
        row.addWidget(self.btn_pick)
        row.addWidget(btn_extent)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Radius"))
        self.radius = QDoubleSpinBox()
        self.radius.setRange(0.5, 250)
        self.radius.setSingleStep(1)
        self.radius.setDecimals(1)
        self.radius.setSuffix(" km")
        self.radius.setValue(
            float(QgsSettings().value(SETTINGS_PREFIX + "radius_km", 5.0))
        )
        self.radius.valueChanged.connect(self._refresh_area)
        row.addWidget(self.radius)
        row.addStretch()
        layout.addLayout(row)
        self.cb_whole = QCheckBox("Whole area (coarser resolution)")
        self.cb_whole.hide()
        self.cb_whole.toggled.connect(self._refresh_area)
        layout.addWidget(self.cb_whole)
        self.area_label = _Message(rich=True)  # escaped
        self.area_label.setText("No area chosen yet.")
        layout.addWidget(self.area_label)

        layout.addWidget(_heading("2. Purpose"))
        self.recipe_group = QButtonGroup(self)
        for n, recipe in enumerate(recipes.RECIPES):
            radio = QRadioButton(recipe.label)
            radio.setProperty("recipe", recipe.key)
            self.recipe_group.addButton(radio, n)
            layout.addWidget(radio)
            layout.addWidget(_hint(recipe.description))
        last = QgsSettings().value(SETTINGS_PREFIX + "recipe", recipes.RECIPES[0].key)
        for button in self.recipe_group.buttons():
            button.setChecked(button.property("recipe") == last)
        if self.recipe_group.checkedButton() is None:
            self.recipe_group.buttons()[0].setChecked(True)

        layout.addWidget(_heading("3. Load"))
        row = QHBoxLayout()
        self.btn_load = QPushButton("Load")
        self.btn_load.setMinimumHeight(32)
        self.btn_load.clicked.connect(self.load)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        row.addWidget(self.btn_load, 1)
        row.addWidget(self.btn_cancel)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = _Message()
        layout.addWidget(self.status)

        self.info = QTextBrowser()
        self.info.setOpenExternalLinks(True)
        self.info.setMinimumHeight(160)
        self.info.hide()
        layout.addWidget(self.info)
        self.btn_copy = QPushButton("Copy attribution")
        self.btn_copy.hide()
        self.btn_copy.clicked.connect(self._copy_attribution)
        layout.addWidget(self.btn_copy)

        layout.addStretch()
        self.folder_label = _hint("")
        self.folder_label.setWordWrap(False)
        layout.addWidget(self.folder_label)
        row = QHBoxLayout()
        btn_expert = QPushButton("Expert mode …")
        btn_expert.setFlat(True)
        btn_expert.clicked.connect(self.open_expert)
        self.btn_help = QPushButton("Help")
        self.btn_help.setFlat(True)
        self.btn_help.setToolTip("Open the documentation in the web browser.")
        self.btn_help.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(stac.HELP_URL))
        )
        self.btn_folder = QPushButton("Change folder …")
        self.btn_folder.setFlat(True)
        self.btn_folder.clicked.connect(self._choose_folder)
        row.addWidget(btn_expert)
        row.addWidget(self.btn_help)
        row.addStretch()
        row.addWidget(self.btn_folder)
        layout.addLayout(row)
        layout.addWidget(_hint(places.OSM_ATTRIBUTION))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.setWidget(scroll)
        self._update_folder_label()

    # -- place ------------------------------------------------------------------------

    def search(self, text: str | None = None):
        if text is not None:
            self.query.setText(text)
        query = self.query.text().strip()
        if not query or self.geocode_task is not None:
            return
        coords = places.parse_coordinates(query)
        if coords:
            lon, lat = coords
            self.results.hide()
            self._applied_query = query
            self.set_place(
                places.Place(f"{lat:.5f}, {lon:.5f}", lon, lat, None, "coordinate")
            )
            return
        task = GeocodeTask(query)
        task.taskCompleted.connect(lambda: self._geocode_done(task))
        task.taskTerminated.connect(lambda: self._geocode_done(task))
        self.geocode_task = task
        self.btn_search.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.place_status.setText("Searching place …")
        QgsApplication.taskManager().addTask(task)

    def _geocode_done(self, task: GeocodeTask):
        self.geocode_task = None
        self.btn_search.setEnabled(True)
        load_next, self._load_after_search = self._load_after_search, False
        self.btn_cancel.setEnabled(self.recipe_task is not None)
        if task.isCanceled():
            self.place_status.setText("Place search cancelled.")
            return
        if task.failure():
            self.place_status.setText(f"Place search failed: {task.failure()}")
            return
        if not task.places:
            self.place_status.setText(
                f'No place "{task.query}" found. Try another spelling or '
                "enter coordinates."
            )
            return
        self._applied_query = task.query
        self.place_status.setText("")
        self.results.blockSignals(True)
        self.results.clear()
        for place in task.places:
            entry = QListWidgetItem(place.name)
            entry.setToolTip(f"{place.name}\n{place.kind}")
            entry.setData(Qt.ItemDataRole.UserRole, place)
            self.results.addItem(entry)
        self.results.blockSignals(False)
        self.results.setVisible(len(task.places) > 1)
        self.results.setCurrentRow(0)
        if len(task.places) == 1:
            self.set_place(task.places[0])
            if load_next:
                self._start_load()
        elif load_next:
            self.place_status.setText('Several places found: choose one, then "Load".')

    def _query_is_new(self) -> bool:
        text = self.query.text().strip()
        return bool(text) and text != self._applied_query

    def _on_result_selected(self, current, _previous):
        if current is not None:
            self.set_place(current.data(Qt.ItemDataRole.UserRole))

    def _pick_on_map(self, checked: bool = True):
        if not checked:
            self._end_pick()
            return
        if self.canvas.mapTool() is not self.map_tool:
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.map_tool)
        self.btn_pick.setChecked(True)
        self.place_status.setText("Click a point on the map …")

    def _end_pick(self):
        if self.canvas.mapTool() is self.map_tool:
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)
            else:
                self.canvas.unsetMapTool(self.map_tool)
        self._previous_tool = None

    def _on_pick_ended(self):
        """The tool lost the canvas, by a finished pick or another tool."""
        self.btn_pick.setChecked(False)
        self.place_status.setText("")

    def _point_on_earth(self, point: QgsPointXY) -> QgsPointXY | None:
        """The click in WGS84, None if the map position is not on the globe."""
        crs = self.canvas.mapSettings().destinationCrs()
        to_wgs = QgsCoordinateTransform(crs, WGS84, QgsProject.instance())
        try:
            wgs = to_wgs.transform(point)
            back = to_wgs.transform(wgs, Qgis.TransformDirection.Reverse)
        except QgsCsException:
            return None
        if places._checked(wgs.x(), wgs.y()) is None:
            return None
        if "+proj=merc" in crs.toProj() and abs(wgs.y()) > MERCATOR_MAX_LAT:
            return None
        # Projections wrap or clamp positions beyond their limits.
        if abs(back.x() - point.x()) + abs(back.y() - point.y()) > 1:
            return None
        return wgs

    def _on_map_click(self, point: QgsPointXY, button):
        if button != Qt.MouseButton.LeftButton:
            return
        wgs = self._point_on_earth(point)
        if wgs is None:
            self.place_status.setText(
                "This point is outside the Earth. Click on land or sea."
            )
            return
        self._end_pick()
        self.place_status.setText("")
        self.results.hide()
        self.query.clear()
        self.set_place(
            places.Place(
                f"Point {wgs.y():.4f}, {wgs.x():.4f}", wgs.x(), wgs.y(), None, "point"
            ),
            zoom=False,
        )

    def _use_canvas_extent(self):
        extent = self.canvas.extent()
        geom = QgsGeometry.fromRect(extent).densifyByCount(20)
        geom.transform(
            QgsCoordinateTransform(
                self.canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance()
            )
        )
        box = geom.boundingBox()
        self.cb_whole.blockSignals(True)
        self.cb_whole.setChecked(False)
        self.cb_whole.blockSignals(False)
        self.cb_whole.hide()
        self.radius.setEnabled(False)
        bbox = (box.yMinimum(), box.yMaximum(), box.xMinimum(), box.xMaximum())
        place = places.Place(
            "Map extent", box.center().x(), box.center().y(), bbox, "extent"
        )
        self.results.hide()
        self.query.clear()
        self.place = place
        width, height = places._extent_km(bbox)
        note = ""
        if max(width, height) > LARGE_AREA_KM:
            note = "Very large area: zoom in on the map for more detail."
        self.area = places.Area(geom.asWkt(), "bbox", width, height, note)
        self._show_area(zoom=False)

    def set_place(self, place: places.Place, zoom: bool = True):
        self.place = place
        self.cb_whole.setChecked(False)
        self._refresh_area(zoom=zoom)

    def _refresh_area(self, *_args, zoom: bool = False):
        if self.place is None or self.place.kind == "extent":
            return
        self.area = places.area_for(
            self.place, self.radius.value(), self.cb_whole.isChecked()
        )
        self.cb_whole.setVisible(bool(self.area.note) or self.cb_whole.isChecked())
        self.radius.setEnabled(self.area.kind == "circle")
        self._show_area(zoom)

    def _show_area(self, zoom: bool):
        area, place = self.area, self.place
        if area is None or place is None:
            return
        kind = "circle" if area.kind == "circle" else "rectangle"
        text = (
            f"<b>{html.escape(places.short_name(place.name))}</b> – {kind}, "
            f"{area.width_km:.1f} × {area.height_km:.1f} km"
        )
        if area.note:
            text += f"<br>{html.escape(area.note)}"
        self.area_label.setText(text)
        geom = QgsGeometry.fromWkt(area.wkt)
        geom.transform(
            QgsCoordinateTransform(
                WGS84, self.canvas.mapSettings().destinationCrs(), QgsProject.instance()
            )
        )
        self.rubber.setToGeometry(geom, None)
        if zoom:
            box = QgsRectangle(geom.boundingBox())
            box.scale(1.3)
            self.canvas.setExtent(box)
            self.canvas.refresh()

    # -- load -------------------------------------------------------------------------

    def _recipe(self) -> recipes.Recipe:
        return recipes.RECIPES_BY_KEY[
            self.recipe_group.checkedButton().property("recipe")
        ]

    def out_root(self) -> str:
        home = QgsProject.instance().homePath()
        if home:
            return os.path.join(home, "SatelliteData")
        return QgsSettings().value(
            SETTINGS_PREFIX + "out_dir",
            os.path.join(os.path.expanduser("~"), "SatelliteDownloads"),
        )

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.out_root()
        )
        if folder:
            QgsSettings().setValue(SETTINGS_PREFIX + "out_dir", folder)
            self._update_folder_label()

    def _update_folder_label(self, *_args):
        in_project = bool(QgsProject.instance().homePath())
        self.btn_folder.setEnabled(not in_project)
        self.btn_folder.setToolTip(
            'With a saved project, data goes to "SatelliteData" in the project folder.'
            if in_project
            else "Choose the download folder used without a saved project."
        )
        root = os.path.normpath(self.out_root())
        metrics = self.folder_label.fontMetrics()
        short = metrics.elidedText(
            root,
            Qt.TextElideMode.ElideMiddle,
            metrics.averageCharWidth() * FOLDER_LABEL_CHARS,
        )
        self.folder_label.setText("Saving to:\n" + short)
        self.folder_label.setToolTip(root)

    def load(self):
        if self.recipe_task is not None:
            return
        # A place that was typed but not confirmed must not load the old area.
        if self.geocode_task is not None or self._query_is_new():
            self._load_after_search = True
            if self.geocode_task is None:
                self.search()
            if self.geocode_task is not None:
                return  # continues in _geocode_done
            self._load_after_search = False
        self._start_load()

    def _start_load(self):
        if self.area is None or self.place is None:
            self.status.setText(
                "First search for a place, click on the map or use the map extent."
            )
            return
        large = max(self.area.width_km, self.area.height_km) > LARGE_AREA_KM
        if large and not confirm(
            self, "Load a large area?", self._large_area_question(), "Load"
        ):
            return
        recipe = self._recipe()
        settings = QgsSettings()
        settings.setValue(SETTINGS_PREFIX + "recipe", recipe.key)
        settings.setValue(SETTINGS_PREFIX + "radius_km", self.radius.value())
        self._update_folder_label()

        task = RecipeTask(recipe, self.area.wkt, self.place.name, self.out_root())
        task.log.connect(self.status.setText)
        task.progressChanged.connect(self._on_progress)
        task.planned.connect(lambda: self._on_planned(task))
        task.taskCompleted.connect(lambda: self._recipe_done(task))
        task.taskTerminated.connect(lambda: self._recipe_done(task))
        self.recipe_task = task
        self._set_running(True)
        self.info.hide()
        self.btn_copy.hide()
        QgsApplication.taskManager().addTask(task)

    def _large_area_question(self) -> str:
        area = self.area
        if area is None:
            return ""
        side_m = max(area.width_km, area.height_km) * 1000
        collection = COLLECTIONS_BY_KEY[self._recipe().sources[0]]
        resolution = recipes._resolution(collection, side_m)
        return (
            f"The area is {area.width_km:.0f} × {area.height_km:.0f} km, so the "
            f"resolution is coarsened to about {resolution:.0f} m "
            f"(native {collection.native_res:g} m). The download may still take "
            "several minutes."
        )

    def _on_progress(self, value: float):
        if value > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(value))

    def _on_planned(self, task: RecipeTask):
        recipe_plan = task.plan
        if recipe_plan is None:
            return
        acq = recipe_plan.acquisition
        self.status.setText(f"Loading {acq.collection.label} from {_date(acq.when)} …")
        handle, target = tempfile.mkstemp(prefix="sd_preview_", suffix=".tif")
        os.close(handle)
        preview = PreviewTask(recipe_plan, target)
        preview.taskCompleted.connect(lambda: self._preview_done(preview, task))
        preview.taskTerminated.connect(lambda: setattr(self, "preview_task", None))
        self.preview_task = preview
        QgsApplication.taskManager().addTask(preview)

    def _preview_done(self, preview: PreviewTask, task: RecipeTask):
        self.preview_task = None
        if self.recipe_task is not task or not preview.path:
            return  # the full result is already there, or no preview possible
        group = QgsProject.instance().layerTreeRoot()
        layer = styling.add_layer(group, preview.path, "Preview (will be replaced)")
        if layer is not None:
            styling.style_rgb(
                layer
            ) if task.recipe.style == "rgb" else styling.style_gray(layer)
            self.preview_layer_id = layer.id()

    def _remove_preview(self):
        if self.preview_layer_id:
            QgsProject.instance().removeMapLayer(self.preview_layer_id)
            self.preview_layer_id = None

    def _recipe_done(self, task: RecipeTask):
        self.recipe_task = None
        self._set_running(False)
        self._remove_preview()
        if task.isCanceled():
            self.status.setText("Cancelled.")
            return
        if task.failure() or task.result is None:
            self.status.setText("")
            self._show_info(
                "<p><b>No data loaded.</b></p>"
                f"<p>{html.escape(task.failure() or '')}</p>"
            )
            return
        result = task.result
        acq = result.plan.acquisition
        name = (
            f"{places.short_name(task.place_name)} – {task.recipe.label} – "
            f"{_date(acq.when)}"
        )
        group = styling.add_group(name)
        main = result.derived.get("db", result.outputs[0])
        if task.recipe.style == "dem":
            _add_terrain_layers(group, main, result.derived)
        else:
            _add_image_layer(group, main, task.recipe.style, acq)
        self.status.setText("Loaded into the Layers panel.")
        self._show_result(result, main)

    def _show_result(self, result: recipes.RecipeResult, main: str):
        recipe_plan = result.plan
        acq = recipe_plan.acquisition
        rows = [
            ("Data", acq.collection.label),
            (
                "Acquired",
                _date(acq.when) if not recipe_plan.recipe.static else "static",
            ),
            ("Resolution", f"{recipe_plan.resolution_m:g} m"),
            ("Scenes", str(len(acq.items))),
        ]
        if acq.clear is not None:
            rows.append(("Clear sky in the area", f"{acq.clear:.0f} %"))
        if acq.coverage is not None:
            rows.append(("Area coverage", f"{acq.coverage:.0f} %"))
        table = "".join(
            f'<tr><td style="padding-right:12px">{html.escape(k)}</td>'
            f"<td><b>{html.escape(v)}</b></td></tr>"
            for k, v in rows
        )
        notes = "".join(f"<p>{html.escape(n)}</p>" for n in recipe_plan.notes)
        folder = os.path.dirname(main)
        self.last_attribution = result.attribution
        self._show_info(
            f'<table cellspacing="0" cellpadding="2">{table}</table>{notes}'
            f'<p><a href="{_folder_link(folder)}">Open folder</a></p>'
            f"<p><small>{html.escape(result.attribution)}</small></p>"
        )
        self.btn_copy.show()

    def _copy_attribution(self):
        QGuiApplication.clipboard().setText(self.last_attribution)
        self.status.setText("Attribution copied.")

    def _show_info(self, text: str):
        self.info.setHtml(text)
        self.info.show()

    def cancel(self):
        for task in (self.recipe_task, self.preview_task, self.geocode_task):
            if task is not None:
                task.cancel()

    def _set_running(self, running: bool):
        self.btn_load.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.progress.setVisible(running)
        self.progress.setRange(0, 0)

    def showEvent(self, event):
        super().showEvent(event)
        self._show_area(zoom=False)  # closing removed the outline from the map

    # The stub names the parameter a0; Qt passes it positionally.
    def closeEvent(self, event):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.rubber.reset(Qgis.GeometryType.Polygon)
        super().closeEvent(event)

    def cleanup(self):
        self.cancel()
        project = QgsProject.instance()
        project.homePathChanged.disconnect(self._update_folder_label)
        project.readProject.disconnect(self._update_folder_label)
        project.cleared.disconnect(self._update_folder_label)
        self.rubber.reset(Qgis.GeometryType.Polygon)
        self.canvas.scene().removeItem(self.rubber)
        if self.canvas.mapTool() is self.map_tool:
            self.canvas.unsetMapTool(self.map_tool)


def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]
