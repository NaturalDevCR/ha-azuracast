"""Translation files must share the same keys."""

from __future__ import annotations

import json
from pathlib import Path
import re

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


URL_PATTERN = re.compile(r"https?://\S+\.\S+")


def _strings(data: object) -> list[str]:
    if isinstance(data, dict):
        return [s for value in data.values() for s in _strings(value)]
    return [data] if isinstance(data, str) else []


def test_url_pattern() -> None:
    assert URL_PATTERN.search("https://radio.example.com")
    assert not URL_PATTERN.search("Base address, including http:// or https://.")


def test_no_urls_in_translations() -> None:
    """Hassfest rejects URLs in translation strings."""
    for path in (BASE / "strings.json", *(BASE / "translations").glob("*.json")):
        found = [s for s in _strings(json.loads(path.read_text())) if URL_PATTERN.search(s)]
        assert not found, f"{path.name}: {found}"
