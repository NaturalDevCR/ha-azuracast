"""Translation files must share the same keys."""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).parents[1] / "custom_components" / "azuracast"


def _keys(data: object, prefix: str = "") -> set[str]:
    if not isinstance(data, dict):
        return {prefix}
    return {k for key, value in data.items() for k in _keys(value, f"{prefix}.{key}")}


def test_translation_trees_match() -> None:
    strings = json.loads((BASE / "strings.json").read_text())
    for name in ("en.json", "es.json"):
        other = json.loads((BASE / "translations" / name).read_text())
        assert _keys(other) == _keys(strings), name


def test_en_matches_strings() -> None:
    assert json.loads((BASE / "translations" / "en.json").read_text()) == json.loads(
        (BASE / "strings.json").read_text()
    )
