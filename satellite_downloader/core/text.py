"""Small helpers for user-facing English text."""

from __future__ import annotations

LOG_TAG = "Satellite Downloader"  # tab in the QGIS log
LOG_HINT = (
    f'Details are in the QGIS log (View → Panels → Log Messages, tab "{LOG_TAG}").'
)

_IRREGULAR = {"category": "categories"}


def or_join(parts: list[str], word: str = "or") -> str:
    """ "a", "a or b", "a, b or c" (``word`` may be "and")."""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} {word} {parts[-1]}"


def plural(count: int, noun: str) -> str:
    """Return ``count`` with ``noun`` in singular or plural, e.g. "1 scene", "3 scenes"."""
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {_IRREGULAR.get(noun, noun + 's')}"
