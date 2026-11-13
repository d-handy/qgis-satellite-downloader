"""QgsTask wrappers that run core functions in the background."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone

from qgis.core import (
    Qgis,
    QgsFeedback,
    QgsMessageLog,
    QgsTask,
)
from qgis.PyQt.QtCore import pyqtSignal

from .core import clouds, geometry, places, preview, recipes, selection, stac
from .core.catalog import COLLECTIONS_BY_KEY, PROVIDERS, Collection
from .core.pipeline import GDAL_HTTP_OPTIONS, Downloader, DownloadJob, DownloadResult
from .core.text import LOG_HINT, LOG_TAG, plural

# Failures that come from data, network or disk rather than from bugs:
# GDAL and STAC errors are RuntimeError, JSON/parameter problems ValueError.
EXPECTED_ERRORS = (RuntimeError, OSError, ValueError)


class _Base(QgsTask):
    log = pyqtSignal(str)

    def __init__(self, description: str):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.error: str | None = None
        # Lets blocking network requests and GDAL callbacks see cancellation.
        self.feedback = QgsFeedback()
        self.feedback.progressChanged.connect(self.setProgress)

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def failure(self) -> str | None:
        """Message for the user if the task failed, None on success or cancel.

        Programming errors are not caught in ``run``; QGIS logs their traceback
        and terminates the task, which is reported here without details.
        """
        if self.error:
            return self.error
        if self.status() == QgsTask.TaskStatus.Terminated and not self.isCanceled():
            return f"Something went wrong in the plugin. {LOG_HINT}"
        return None

    def _log(self, msg: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info):
        QgsMessageLog.logMessage(msg, LOG_TAG, level)
        self.log.emit(msg)

    def _log_detail(self, msg: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info):
        """For the QGIS log only: too technical for a status line."""
        QgsMessageLog.logMessage(msg, LOG_TAG, level)


class SearchTask(_Base):
    """Search one or many collections; items are tagged with their collection."""

    def __init__(
        self,
        collections: list[Collection],
        geometry_wgs84: dict,
        start: str,
        end: str,
        max_cloud: float | None,
        max_items_each: int,
    ):
        label = (
            collections[0].label
            if len(collections) == 1
            else plural(len(collections), "dataset")
        )
        super().__init__(f"STAC search: {label}")
        self.collections = collections
        self.geometry = geometry_wgs84
        self.start, self.end = start, end
        self.max_cloud = max_cloud
        self.max_items_each = max_items_each
        self.outcome = selection.SearchOutcome()

    @property
    def items(self) -> list[dict]:
        return self.outcome.items

    @property
    def discarded(self) -> int:
        return self.outcome.discarded

    def run(self) -> bool:
        done = 0

        def on_done(collection, count, error):
            nonlocal done
            done += 1
            self.feedback.setProgress(100 * done / len(self.collections))
            if error:
                self._log(
                    f"{collection.label}: search failed ({error[:120]})",
                    Qgis.MessageLevel.Warning,
                )
            elif len(self.collections) > 1:
                self._log(f"{collection.label}: {plural(count, 'scene')}")

        self.outcome = selection.search_many(
            self.collections,
            self.geometry,
            self.start,
            self.end,
            self.max_cloud,
            self.max_items_each,
            self.feedback,
            on_done,
        )
        if self.outcome.errors and len(self.outcome.errors) == len(self.collections):
            self.error = next(iter(self.outcome.errors.values()))
            return False
        return not self.isCanceled()


class ClearSkyTask(_Base):
    """Fills ``stac.CLEAR_KEY`` for items of any collections with a mask band.

    Emits ``evaluated`` with the item's uid (see ``selection.UID_KEY``).
    """

    evaluated = pyqtSignal(str, float)  # item uid, clear % (NaN if unknown)

    def __init__(self, items: list[dict], geometry_wgs84: dict):
        super().__init__("Cloud assessment")
        self.items = [
            it for it in items if selection.item_collection(it).mask is not None
        ]
        self.aoi = geometry.from_geojson(geometry.unwrap_antimeridian(geometry_wgs84))

    def run(self) -> bool:
        done = 0

        def on_result(item, value):
            nonlocal done
            done += 1
            self.feedback.setProgress(100 * done / max(1, len(self.items)))
            self.evaluated.emit(
                item[selection.UID_KEY], float("nan") if value is None else value
            )

        groups: dict[str, list[dict]] = {}
        for item in self.items:
            groups.setdefault(item[selection.COLLECTION_KEY], []).append(item)
        for key, items in groups.items():
            if self.isCanceled():
                break
            collection = COLLECTIONS_BY_KEY[key]
            try:
                clouds.estimate(
                    items,
                    collection,
                    self.aoi,
                    stac.signer_for(PROVIDERS[collection.provider]),
                    GDAL_HTTP_OPTIONS,
                    self.feedback,
                    on_result,
                )
            except EXPECTED_ERRORS as exc:
                self._log(
                    f"{collection.label}: cloud assessment failed ({exc})",
                    Qgis.MessageLevel.Warning,
                )
        return not self.isCanceled()


class GeocodeTask(_Base):
    def __init__(self, query: str):
        super().__init__(f"Place search: {query}")
        self.query = query
        self.places: list[places.Place] = []

    def run(self) -> bool:
        try:
            self.places = places.geocode(self.query, self.feedback)
        except EXPECTED_ERRORS as exc:
            self.error = str(exc)
            return False
        return not self.isCanceled()


class RecipeTask(_Base):
    """Plan and run a recipe; ``planned`` fires once the acquisition is chosen."""

    planned = pyqtSignal()

    def __init__(
        self, recipe: recipes.Recipe, aoi_wkt: str, place_name: str, out_root: str
    ):
        super().__init__(f"{recipe.label}: {place_name}")
        self.recipe = recipe
        self.aoi_wkt = aoi_wkt
        self.place_name = place_name
        self.out_root = out_root
        self.plan: recipes.RecipePlan | None = None
        self.result: recipes.RecipeResult | None = None

    def run(self) -> bool:
        try:
            self.plan = recipes.plan(
                self.recipe,
                self.aoi_wkt,
                datetime.now(timezone.utc).date(),
                self.feedback,
                self._log,
            )
            if self.isCanceled():
                return False
            self.planned.emit()
            self.result = recipes.run(
                self.plan,
                self.out_root,
                self.place_name,
                self.feedback,
                self._log_detail,
                self._log,
            )
        except recipes.NoDataError as exc:
            self.error = str(exc)
            return False
        except EXPECTED_ERRORS as exc:
            if not self.isCanceled():
                self.error = f"Loading failed: {exc}"
                QgsMessageLog.logMessage(
                    traceback.format_exc(), LOG_TAG, Qgis.MessageLevel.Critical
                )
            return False
        return not self.isCanceled()


class PreviewTask(_Base):
    """Server-side cropped preview (display only, never saved as a result)."""

    def __init__(self, recipe_plan: recipes.RecipePlan, target: str):
        super().__init__("Preview")
        self.recipe_plan = recipe_plan
        self.target = target
        self.path: str | None = None

    def run(self) -> bool:
        try:
            self.path = preview.fetch(self.recipe_plan, self.target, self.feedback)
        except EXPECTED_ERRORS as exc:
            self.error = str(exc)
            return False
        return self.path is not None and not self.isCanceled()


class _StepFeedback(QgsFeedback):
    """Maps a sub-job's 0-100 % onto its share of the parent's progress.

    QgsProcessingMultiStepFeedback crashed QGIS when driven from the task's
    worker thread, so the scaling is done here in Python.
    """

    def __init__(self, parent: QgsFeedback, step: int, steps: int):
        super().__init__()
        self._parent = parent
        self._offset = 100 * step / steps
        self._span = 1 / steps
        if parent.isCanceled():
            self.cancel()
        parent.canceled.connect(self.cancel)

    def setProgress(self, progress: float) -> None:
        super().setProgress(progress)
        self._parent.setProgress(self._offset + progress * self._span)


class DownloadTask(_Base):
    """Run one download job per collection, one after another."""

    def __init__(self, jobs: list[DownloadJob]):
        label = (
            jobs[0].collection.label if len(jobs) == 1 else plural(len(jobs), "dataset")
        )
        super().__init__(f"Download: {label}")
        self.jobs = jobs
        self.results: list[tuple[DownloadJob, DownloadResult]] = []
        self.errors: dict[str, str] = {}

    def run(self) -> bool:
        for n, job in enumerate(self.jobs):
            if self.isCanceled():
                return False
            steps = _StepFeedback(self.feedback, n, len(self.jobs))
            if len(self.jobs) > 1:
                self._log(
                    f"— {job.collection.label}: {plural(len(job.items), 'scene')}"
                )
            downloader = Downloader(job, steps, self._log)
            try:
                downloader.run()
            except EXPECTED_ERRORS as exc:
                if self.isCanceled():
                    return False
                self.errors[job.collection.key] = str(exc)
                self._log(f"{job.collection.label}: {exc}", Qgis.MessageLevel.Warning)
            self.results.append((job, downloader.result))
        if self.errors and len(self.errors) == len(self.jobs):
            self.error = next(iter(self.errors.values()))
            return False
        return not self.isCanceled()

    @property
    def outputs(self) -> list[str]:
        return [path for _, result in self.results for path in result.outputs]

    @property
    def failures(self) -> int:
        return sum(r.failures for _, r in self.results) + len(self.errors)

    @property
    def empty(self) -> int:
        return sum(r.empty for _, r in self.results)
