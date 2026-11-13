from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

MENU = "&Satellite Downloader"


class SatelliteDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action: QAction | None = None
        self.expert_action: QAction | None = None
        self.dock = None
        self.dialog = None
        self.locator = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Satellite data …", self.iface.mainWindow())
        self.action.setToolTip(
            "Load satellite data for a place (place → purpose → load)"
        )
        self.action.triggered.connect(self.show_dock)
        self.expert_action = QAction(icon, "Expert mode …", self.iface.mainWindow())
        self.expert_action.triggered.connect(self.show_expert)
        self.iface.addRasterToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)
        self.iface.addPluginToRasterMenu(MENU, self.expert_action)

        from .gui.locator import PlaceLocatorFilter

        self.locator = PlaceLocatorFilter(self.open_place)
        self.iface.registerLocatorFilter(self.locator)

    def unload(self):
        if self.locator is not None:
            self.iface.deregisterLocatorFilter(self.locator)
            self.locator = None
        if self.dock is not None:
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.dialog is not None:
            self.dialog.cancel_tasks()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        for action in (self.action, self.expert_action):
            self.iface.removeRasterToolBarIcon(action)
            self.iface.removePluginRasterMenu(MENU, action)

    def show_dock(self):
        if self.dock is None:
            from .gui.simple_dock import SimpleDock

            self.dock = SimpleDock(self.iface, self.show_expert)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.show()
        self.dock.raise_()

    def open_place(self, text: str):
        self.show_dock()
        if self.dock is not None:
            self.dock.search(text)

    def show_expert(self):
        if self.dialog is None:
            from .gui.expert_dialog import DownloaderDialog

            self.dialog = DownloaderDialog(self.iface)
        else:
            canvas = self.iface.mapCanvas()
            self.dialog.extent_widget.setCurrentExtent(
                canvas.extent(), canvas.mapSettings().destinationCrs()
            )
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
