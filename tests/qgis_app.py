"""Start one headless QgsApplication for the whole test run."""

import os
import sys
import tempfile
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Tests write settings (e.g. the output folder); keep them out of the real profile.
os.environ.setdefault(
    "QGIS_CUSTOM_CONFIG_PATH",
    os.path.join(tempfile.gettempdir(), "satellite_downloader_tests"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.simplefilter("ignore", FutureWarning)

from qgis.core import QgsApplication

_app = None


def start() -> QgsApplication:
    global _app
    if _app is None:
        _app = QgsApplication([], False)
        _app.initQgis()
    return _app


LIVE = os.environ.get("SD_LIVE") == "1"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_output")
