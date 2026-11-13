"""Make loaded results look right immediately and play in the temporal controller."""

from __future__ import annotations

from datetime import datetime

from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsDateTimeRange,
    QgsLayerTreeGroup,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
)
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime
from qgis.PyQt.QtGui import QPainter


def _enum(*candidates):
    """First existing enum value; names moved to ``Qgis`` in later QGIS releases."""
    for owner, path in candidates:
        value = owner
        try:
            for part in path.split("."):
                value = getattr(value, part)
            return value
        except AttributeError:
            continue
    raise AttributeError(candidates)


STRETCH = _enum(
    (QgsContrastEnhancement, "ContrastEnhancementAlgorithm.StretchToMinimumMaximum"),
    (QgsContrastEnhancement, "StretchToMinimumMaximum"),
)
CUMULATIVE_CUT = _enum(
    (Qgis, "RasterRangeLimit.CumulativeCut"),
    (QgsRasterMinMaxOrigin, "Limits.CumulativeCut"),
    (QgsRasterMinMaxOrigin, "CumulativeCut"),
)


def _stretch(layer: QgsRasterLayer) -> None:
    layer.setContrastEnhancement(STRETCH, CUMULATIVE_CUT)


def style_rgb(layer: QgsRasterLayer) -> None:
    if layer.bandCount() < 3:
        style_gray(layer)
        return
    layer.setRenderer(QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3))
    _stretch(layer)


def style_gray(layer: QgsRasterLayer, band: int = 1) -> None:
    layer.setRenderer(QgsSingleBandGrayRenderer(layer.dataProvider(), band))
    _stretch(layer)


def style_ramp(layer: QgsRasterLayer, ramp_name: str, invert: bool = False) -> None:
    ds = gdal.Open(layer.source())  # keep the dataset alive while using its band
    minmax = ds.GetRasterBand(1).ComputeRasterMinMax(False, can_return_none=True)
    ds = None
    ramp = QgsStyle.defaultStyle().colorRamp(ramp_name)
    if ramp is None or minmax is None:
        style_gray(layer)
        return
    if invert:
        ramp.invert()
    shader_fn = QgsColorRampShader(minmax[0], minmax[1], ramp)
    shader_fn.classifyColorRamp(10)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_fn)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader))


def style_hillshade_overlay(layer: QgsRasterLayer) -> None:
    style_gray(layer)
    layer.renderer().setOpacity(0.5)
    layer.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)


def set_acquisition_time(layer: QgsRasterLayer, iso_datetime: str) -> None:
    """Show the layer on its acquisition day in the temporal controller."""
    try:
        when = datetime.fromisoformat(iso_datetime.replace("Z", "+00:00"))
    except ValueError:
        return
    props = layer.temporalProperties()
    props.setMode(
        _enum(
            (Qgis, "RasterTemporalMode.FixedTemporalRange"),
            (type(props), "TemporalMode.ModeFixedTemporalRange"),
            (type(props), "ModeFixedTemporalRange"),
        )
    )
    day = QDate(when.year, when.month, when.day)  # acquisition day in UTC
    props.setFixedTemporalRange(
        QgsDateTimeRange(
            QDateTime(day, QTime(0, 0, 0), Qt.TimeSpec.UTC),
            QDateTime(day, QTime(23, 59, 59), Qt.TimeSpec.UTC),
        )
    )
    props.setIsActive(True)


def add_group(name: str) -> QgsLayerTreeGroup:
    return QgsProject.instance().layerTreeRoot().insertGroup(0, name)


def add_layer(
    group: QgsLayerTreeGroup, path: str, name: str, visible: bool = True
) -> QgsRasterLayer | None:
    layer = QgsRasterLayer(path, name)
    if not layer.isValid():
        return None
    QgsProject.instance().addMapLayer(layer, False)
    group.addLayer(layer).setItemVisibilityChecked(visible)
    return layer
