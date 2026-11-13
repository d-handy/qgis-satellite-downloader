"""Searchable, grouped chooser for many collections.

Two ways to choose:

- click a row: the *current* collection, with full control over its bands;
- tick rows or whole categories: several sensors are searched together, each
  with its default bands and native resolution. Ticks survive filtering, so a
  user can tick "Radar", search "landsat" and tick more.
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.gui import QgsFilterLineEdit
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import Collection, Provider
from ..core.selection import BBox, covers
from ..core.text import plural

__all__ = ["CollectionPicker", "covers", "matches", "period"]

PROVIDER_COLUMN_WIDTH = 120
# Up to this many matches, or with search words, every category is open.
_OPEN_ALL_UP_TO = 40
_CATEGORY_ROLE = Qt.ItemDataRole.UserRole + 1


def _order(collection: Collection) -> tuple:
    """Categories alphabetically with "Other" last, then by name."""
    return (
        collection.category == "Other",
        collection.category,
        collection.label.lower(),
    )


def period(collection: Collection) -> str:
    if not collection.time_filter:
        return "static"
    start, end = collection.interval
    if not start:
        return f"until {end[:4]}" if end else "n/a"
    return f"{start[:4]}–{(end or '')[:4] or 'today'}"


def matches(collection: Collection, provider: Provider, words: list[str]) -> bool:
    # Asset and band names make product contents findable, e.g. "NDVI" for the
    # MODIS vegetation indices whose title never mentions it.
    text = (
        f"{collection.label} {collection.collection_id} {collection.category} "
        f"{provider.label} {collection.note} {' '.join(collection.assets)} "
        f"{' '.join(collection.bands)}"
    ).lower()
    return all(w in text for w in words)


def _category_row(category: str) -> QTreeWidgetItem:
    """Tickable category row; ticking it ticks all its datasets."""
    group = QTreeWidgetItem([category])
    group.setData(0, _CATEGORY_ROLE, category)
    group.setFlags(
        Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsUserCheckable
        | Qt.ItemFlag.ItemIsAutoTristate
    )
    return group


class CollectionPicker(QWidget):
    currentChanged = pyqtSignal(str)  # collection key
    checkedChanged = pyqtSignal(list)  # ticked collection keys

    def __init__(
        self,
        collections: list[Collection],
        providers: dict[str, Provider],
        area_bbox: Callable[[], BBox | None],
        parent=None,
    ):
        super().__init__(parent)
        self._collections = collections
        self._providers = providers
        self._area_bbox = area_bbox
        self._current: str | None = None
        self._checked: set[str] = set()
        self._open_categories: set[str] = set()  # expanded by the user
        self._auto_expanded = False  # last view expanded everything by itself
        self._last_words: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QgsFilterLineEdit()
        self.search.setPlaceholderText(
            "Search satellite, dataset or topic, e.g. CBERS, MODIS, NDVI …"
        )
        self.search.textChanged.connect(self.refresh)
        self.only_here = QCheckBox("Only for my area")
        self.only_here.setToolTip(
            "Hides datasets whose extent does not touch the area."
        )
        self.only_here.toggled.connect(self.refresh)
        row.addWidget(self.search, 1)
        row.addWidget(self.only_here)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Dataset", "Resolution", "Period", "Provider"])
        self.tree.setMinimumHeight(220)
        self.tree.setUniformRowHeights(True)
        self.tree.setToolTip(
            "Click a row: one dataset with free choice of bands.\n"
            "Tick rows (or a whole category): search several datasets together."
        )
        # Many names share their start ("Sentinel-2 L2A ..."); their end tells them apart.
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(3, PROVIDER_COLUMN_WIDTH)
        self.tree.currentItemChanged.connect(self._on_item_changed)
        self.tree.itemChanged.connect(self._on_check_changed)
        layout.addWidget(self.tree)
        self.empty_label = QLabel()
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)  # search text
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

        row = QHBoxLayout()
        self.checked_label = QLabel()
        self.btn_clear = QPushButton("Clear ticks")
        self.btn_clear.clicked.connect(self.clear_checked)
        row.addWidget(self.checked_label, 1)
        row.addWidget(self.btn_clear)
        layout.addLayout(row)
        self.refresh()

    # -- current (single) collection --------------------------------------------------

    def currentKey(self) -> str | None:
        return self._current

    def setCurrentKey(self, key: str) -> None:
        for item in self._leaves():
            if item.data(0, Qt.ItemDataRole.UserRole) == key:
                self.tree.setCurrentItem(item)
                return

    # -- ticked collections -----------------------------------------------------------

    def checkedKeys(self) -> list[str]:
        order = {c.key: n for n, c in enumerate(self._collections)}
        return sorted(self._checked, key=lambda k: order.get(k, 0))

    def setCheckedKeys(self, keys) -> None:
        self._checked = set(keys)
        self.refresh()
        self._emit_checked()

    def clear_checked(self) -> None:
        self.setCheckedKeys([])

    def count(self) -> int:
        return len(self._leaves())

    def _leaves(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            out.extend(group.child(j) for j in range(group.childCount()))
        return out

    def categoryItem(self, category: str) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if group.data(0, _CATEGORY_ROLE) == category:
                return group
        return None

    def refresh(self, *_args) -> None:
        words = self.search.text().lower().split()
        area = self._area_bbox() if self.only_here.isChecked() else None
        if not self._auto_expanded:
            self._open_categories = self._expanded_categories()
        scroll = self.tree.verticalScrollBar().value()
        same_view = words == self._last_words
        self._last_words = words
        shown = self._matching(words, area)
        self.tree.blockSignals(True)
        self.tree.clear()
        groups, selected = self._fill_tree(shown)
        open_all = bool(words) or len(shown) <= _OPEN_ALL_UP_TO
        for group in groups.values():
            group.setText(0, f"{group.text(0)} ({group.childCount()})")
            group.setExpanded(open_all or self._keep_open(group, selected))
        self._auto_expanded = open_all
        self.tree.blockSignals(False)
        if selected is not None:
            self.tree.setCurrentItem(selected)
        if same_view:
            self.tree.verticalScrollBar().setValue(scroll)
        self._update_label()
        self._update_empty_label(shown)

    def _matching(self, words: list[str], area) -> list[Collection]:
        return [
            c
            for c in self._collections
            if matches(c, self._providers[c.provider], words) and covers(c, area)
        ]

    def _keep_open(
        self, group: QTreeWidgetItem, selected: QTreeWidgetItem | None
    ) -> bool:
        """Opened by the user before, or holding the current dataset."""
        if group.data(0, _CATEGORY_ROLE) in self._open_categories:
            return True
        return selected is not None and selected.parent() is group

    def _expanded_categories(self) -> set[str]:
        """Categories the user opened, kept across refreshes."""
        return {
            self.tree.topLevelItem(i).data(0, _CATEGORY_ROLE)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
        }

    def _fill_tree(
        self, shown: list[Collection]
    ) -> tuple[dict[str, QTreeWidgetItem], QTreeWidgetItem | None]:
        """Category rows with their datasets; also returns the current row."""
        groups: dict[str, QTreeWidgetItem] = {}
        selected = None
        for c in sorted(shown, key=_order):
            group = groups.get(c.category)
            if group is None:
                group = _category_row(c.category)
                self.tree.addTopLevelItem(group)
                groups[c.category] = group
            item = self._dataset_row(c)
            group.addChild(item)
            if c.key == self._current:
                selected = item
        return groups, selected

    def _dataset_row(self, c: Collection) -> QTreeWidgetItem:
        provider = self._providers[c.provider].label
        item = QTreeWidgetItem([c.label, f"{c.native_res:g} m", period(c), provider])
        item.setData(0, Qt.ItemDataRole.UserRole, c.key)
        item.setToolTip(0, f"{c.label}\n{c.collection_id}\n{c.note}")
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        ticked = c.key in self._checked
        item.setCheckState(
            0, Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        )
        return item

    def _update_empty_label(self, shown: list[Collection]) -> None:
        text = self.search.text().strip()
        if shown:
            message = ""
        elif text:
            message = (
                f'No dataset matches "{text}". Try a satellite, product or band '
                "name, e.g. Sentinel-2 or NDVI."
            )
        else:
            message = (
                'No dataset covers this area. Untick "Only for my area" to see '
                "all datasets."
            )
        self.empty_label.setText(message)
        self.empty_label.setVisible(bool(message))

    def _on_item_changed(self, current, _previous):
        if current is None:
            return
        key = current.data(0, Qt.ItemDataRole.UserRole)
        if key and key != self._current:
            self._current = key
            self.currentChanged.emit(key)

    def _on_check_changed(self, item: QTreeWidgetItem, column: int):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if column != 0 or not key:
            return  # category rows propagate to their children via auto-tristate
        ticked = item.checkState(0) == Qt.CheckState.Checked
        if ticked == (key in self._checked):
            return
        if ticked:
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self._emit_checked()

    def _emit_checked(self):
        self._update_label()
        self.checkedChanged.emit(self.checkedKeys())

    def _update_label(self):
        n = len(self._checked)
        self.checked_label.setText(
            f"{plural(n, 'dataset')} ticked"
            if n
            else "Tick rows to search several together"
        )
        self.btn_clear.setEnabled(bool(n))
