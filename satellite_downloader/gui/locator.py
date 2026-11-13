"""QGIS search bar entry: ``sat Atacama`` opens the simple mode for that place.

The filter offers one result per text without contacting Nominatim, because
the locator asks on every keystroke and Nominatim forbids that usage. The
actual search starts only when the entry is chosen.
"""

from __future__ import annotations

from qgis.core import QgsLocatorFilter, QgsLocatorResult


class PlaceLocatorFilter(QgsLocatorFilter):
    def __init__(self, open_place):
        super().__init__()
        self._open_place = open_place

    def clone(self):
        return PlaceLocatorFilter(self._open_place)

    def name(self):
        return "satellite_downloader"

    def displayName(self):
        return "Satellite data for a place"

    def prefix(self):
        return "sat"

    def fetchResults(self, string, context, feedback):
        text = string.strip()
        if len(text) < 2:
            return
        result = QgsLocatorResult(self, f'Satellite data for "{text}" …', text)
        self.resultFetched.emit(result)

    def triggerResult(self, result):
        data = (
            result.userData()
            if callable(getattr(result, "userData", None))
            else result.userData
        )
        self._open_place(str(data))
