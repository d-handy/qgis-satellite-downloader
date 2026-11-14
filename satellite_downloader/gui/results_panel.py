"""Result list of the expert dialog: tickable scenes with their key numbers.

The panel shows what it is given and reports tick changes; which scenes
exist, and what the numbers mean for the next step, stays with the dialog.
"""

from __future__ import annotations

import json
import math

from osgeo import ogr
from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import selection
from ..core.catalog import PROVIDERS
from ..core.stac import CLEAR_KEY, COVERAGE_KEY
from .compat import double_field, string_field

COL_CHECK, COL_DATE, COL_CLEAR, COL_COVERAGE, COL_SCENE_CLOUD, COL_SENSOR, COL_ID = (
    range(7)
)
_TITLES = {
    COL_CHECK: "",
    COL_DATE: "Date (UTC)",
    COL_CLEAR: "Clear %",
    COL_COVERAGE: "Coverage %",
    COL_SCENE_CLOUD: "Cloud %",
    COL_SENSOR: "Dataset",
    COL_ID: "Scene",
}
_TOOLTIPS = {
    COL_CLEAR: "Share of cloud-free pixels in the area, from the scene's mask band "
    "(computed in the background after the search)",
    COL_COVERAGE: "Share of the area covered by the scene footprint",
    COL_SCENE_CLOUD: "Cloud cover of the whole scene according to the catalogue",
}


def number_item(value: float | None, decimals: int) -> QTableWidgetItem:
    """Cell that sorts numerically; empty when the value is unknown."""
    cell = QTableWidgetItem()
    if value is not None and not math.isnan(value):
        cell.setData(Qt.ItemDataRole.DisplayRole, round(float(value), decimals))
    return cell


def _check_state(state: bool) -> Qt.CheckState:
    return Qt.CheckState.Checked if state else Qt.CheckState.Unchecked


class ResultsPanel(QWidget):
    """Scene table with "All"/"None" and a summary line below it."""

    ticksChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = self._make_table()
        layout.addWidget(self.table)
        row = QHBoxLayout()
        btn_all = QPushButton("All")
        btn_all.clicked.connect(lambda: self.check_all(True))
        btn_none = QPushButton("None")
        btn_none.clicked.connect(lambda: self.check_all(False))
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addWidget(self.summary_label, 1)
        layout.addLayout(row)

    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, len(_TITLES))
        table.setHorizontalHeaderLabels([_TITLES[c] for c in range(len(_TITLES))])
        table.verticalHeader().hide()
        for col, tip in _TOOLTIPS.items():
            table.horizontalHeaderItem(col).setToolTip(tip)
        header = table.horizontalHeader()
        for col in range(COL_ID):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_ID, QHeaderView.ResizeMode.Stretch)
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.itemChanged.connect(lambda *_: self.ticksChanged.emit())
        return table

    def set_items(self, items: list[dict]) -> None:
        """One ticked row per scene; the dataset column only for several datasets."""
        table = self.table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(items))
        for row, item in enumerate(items):
            self._fill_row(row, item)
        table.setColumnHidden(
            COL_SENSOR, len({it[selection.COLLECTION_KEY] for it in items}) < 2
        )
        table.setSortingEnabled(True)
        table.blockSignals(False)
        self.ticksChanged.emit()

    def _fill_row(self, row: int, item: dict) -> None:
        props = item["properties"]
        collection = selection.item_collection(item)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked)
        chk.setData(Qt.ItemDataRole.UserRole, item[selection.UID_KEY])
        date = (props.get("datetime") or props.get("start_datetime") or "")[:19]
        sensor = QTableWidgetItem(collection.label)
        sensor.setToolTip(
            f"{collection.label}\n{PROVIDERS[collection.provider].label} · "
            f"{collection.native_res:g} m"
        )
        cells = {
            COL_CHECK: chk,
            COL_DATE: QTableWidgetItem(date.replace("T", " ")),
            COL_SENSOR: sensor,
            COL_CLEAR: number_item(item.get(CLEAR_KEY), 0),
            COL_COVERAGE: number_item(item.get(COVERAGE_KEY), 0),
            COL_SCENE_CLOUD: number_item(props.get("eo:cloud_cover"), 1),
            COL_ID: QTableWidgetItem(item["id"]),
        }
        for col, cell in cells.items():
            self.table.setItem(row, col, cell)

    def check_all(self, state: bool) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, COL_CHECK).setCheckState(_check_state(state))
        self.table.blockSignals(False)
        self.ticksChanged.emit()

    def checked_uids(self) -> list[str]:
        """Ticked scenes in the order shown (the table may be sorted)."""
        return [
            self.table.item(r, COL_CHECK).data(Qt.ItemDataRole.UserRole)
            for r in range(self.table.rowCount())
            if self.table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked
        ]

    def set_clear(self, uid: str, value: float | None) -> None:
        """Fill in the clear-sky share of one scene once it is known."""
        row = self._row_for(uid)
        if row is None:
            return
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setItem(row, COL_CLEAR, number_item(value, 0))
        self.table.setSortingEnabled(sorting)

    def _row_for(self, uid: str) -> int | None:
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, COL_CHECK)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == uid:
                return row
        return None


def footprints_layer(items: list[dict]) -> QgsVectorLayer:
    """Memory layer with one semi-transparent footprint per scene."""
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", "Footprints", "memory")
    pr = layer.dataProvider()
    pr.addAttributes(
        [
            string_field("sensor"),
            string_field("id"),
            string_field("datetime"),
            double_field("cloud"),
        ]
    )
    layer.updateFields()
    feats = []
    for item in items:
        if not item.get("geometry"):
            continue
        ogr_geom = ogr.CreateGeometryFromJson(json.dumps(item["geometry"]))
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(ogr_geom.ExportToWkt()))
        props = item["properties"]
        f.setAttributes(
            [
                selection.item_collection(item).label,
                item["id"],
                props.get("datetime") or props.get("start_datetime"),
                props.get("eo:cloud_cover"),
            ]
        )
        feats.append(f)
    pr.addFeatures(feats)
    layer.updateExtents()
    layer.renderer().symbol().setOpacity(0.35)
    return layer
