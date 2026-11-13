"""Small shims for API differences between QGIS 3.34 and later (Qt6-ready)."""

from qgis.core import Qgis, QgsField

if Qgis.QGIS_VERSION_INT >= 33800:
    from qgis.PyQt.QtCore import QMetaType

    def string_field(name: str) -> QgsField:
        return QgsField(name, QMetaType.Type.QString)

    def double_field(name: str) -> QgsField:
        return QgsField(name, QMetaType.Type.Double)
else:  # QGIS < 3.38 only accepts QVariant types
    from qgis.PyQt.QtCore import QVariant

    def string_field(name: str) -> QgsField:
        return QgsField(name, QVariant.String)

    def double_field(name: str) -> QgsField:
        return QgsField(name, QVariant.Double)
