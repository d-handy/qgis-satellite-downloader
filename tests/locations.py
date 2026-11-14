"""Location test suite: core recipes at 20 places plus open sea.

Each run records whether a product or an explanation came back, the source,
acquisition date, clear share, duration and file size.

Usage: python-qgis.bat tests/locations.py [report.md]
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import qgis_app

qgis_app.start()

from osgeo import gdal
from qgis.core import QgsFeedback

from satellite_downloader.core import places, recipes

gdal.UseExceptions()

TODAY = datetime.now(timezone.utc).date()

RADIUS_KM = 2.0
LOCATIONS = [
    ("Cologne", 6.95, 50.94),
    ("Atacama", -69.9, -24.4),
    ("Manaus", -60.02, -3.1),
    ("Nairobi", 36.82, -1.29),
    ("Cape Town", 18.42, -33.92),
    ("Sahara", 2.0, 25.0),
    ("Sydney", 151.2, -33.87),
    ("Delhi", 77.2, 28.61),
    ("Beijing", 116.4, 39.9),
    ("Tokyo", 139.69, 35.69),
    ("New York", -73.97, 40.78),
    ("Hawaii", -155.58, 19.6),
    ("Zurich", 8.54, 47.37),
    ("Svalbard", 15.6, 78.22),
    ("Greenland", -50.7, 67.0),
    ("McMurdo", 166.67, -77.85),
    ("Fiji", 179.95, -16.78),
    ("Kamchatka", 158.65, 53.02),
    ("Himalaya", 86.92, 27.99),
    ("Iceland", -21.9, 64.14),
    ("North Atlantic", -35.0, 35.0),
]


def run_one(recipe, lon, lat, name):
    wkt = places.circle(lon, lat, RADIUS_KM)
    row = {"place": name, "recipe": recipe.key}
    messages = []
    t = time.monotonic()
    try:
        plan = recipes.plan(recipe, wkt, TODAY, QgsFeedback())
        acq = plan.acquisition
        row.update(
            source=acq.collection.key,
            date=acq.when[:10],
            scenes=len(acq.items),
            clear=None if acq.clear is None else round(acq.clear),
            notes=plan.notes,
        )
        result = recipes.run(
            plan,
            os.path.join(qgis_app.OUTPUT, "locations"),
            name,
            QgsFeedback(),
            lambda m, _l: messages.append(m),
        )
        row["mb"] = round(sum(os.path.getsize(p) for p in result.outputs) / 1e6, 2)
        row["result"] = "product"
    except recipes.NoDataError as exc:
        row["result"] = "explanation"
        row["notes"] = [str(exc)]
    except Exception as exc:  # noqa: BLE001 - the suite reports every failure per place
        row["result"] = "ERROR"
        row["notes"] = [f"{type(exc).__name__}: {exc}"]
    row["seconds"] = round(time.monotonic() - t, 1)
    return row


def main():
    rows = []
    for name, lon, lat in LOCATIONS:
        for recipe in recipes.RECIPES:
            row = run_one(recipe, lon, lat, name)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    target = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(qgis_app.OUTPUT, "locations.md")
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(
            f"# Location test suite ({TODAY:%Y-%m-%d}, radius {RADIUS_KM:g} km)\n\n"
        )
        fh.write(
            "| Place | Recipe | Result | Source | Acquired | clear % | s | MB | Notes |\n"
        )
        fh.write("|---|---|---|---|---|---|---|---|---|\n")
        fh.writelines(
            f"| {r['place']} | {r['recipe']} | {r['result']} | {r.get('source', '')} | "
            f"{r.get('date', '')} | {'' if r.get('clear') is None else r['clear']} | "
            f"{r['seconds']} | {r.get('mb', '')} | "
            f"{' '.join(r.get('notes') or []).replace('|', '/')} |\n"
            for r in rows
        )
    with open(os.path.splitext(target)[0] + ".json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    failures = [r for r in rows if r["result"] == "ERROR"]
    print(f"\n{len(rows)} runs, {len(failures)} errors")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
