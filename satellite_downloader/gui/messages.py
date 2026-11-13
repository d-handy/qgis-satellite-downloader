"""Message boxes whose buttons name what happens."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QMessageBox, QWidget


def confirm(parent: QWidget, title: str, text: str, action: str) -> bool:
    """Ask before a slow or large action; True if the user chose ``action``.

    The buttons read e.g. "Download" / "Cancel" instead of "Yes" / "No", so the
    choice is clear without reading the question again.
    """
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    go = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is go
