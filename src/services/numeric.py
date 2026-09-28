"""Deterministic numeric normalization for evidence-fidelity gates."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_KANJI = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_LARGE = {"万": 10_000, "億": 100_000_000}
_UNIT = {"%": ("percent", "percent"), "パーセント": ("percent", "percent"), "円": ("currency", "jpy"), "件": ("count", "count"), "台": ("count", "count"), "日": ("duration", "day"), "月": ("duration", "month"), "年": ("duration", "year")}
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?|[〇一二三四五六七八九十百千万億]+")
_UNIT_SUFFIX = re.compile(r"(?:%|パーセント|円|件|台|日|月|年)")


def numeric_vocabulary(text: str) -> bool:
    return bool(re.search(r"[0-9〇一二三四五六七八九十百千万億]", unicodedata.normalize("NFKC", text)))


def extract_numeric_tokens(text: str) -> list[dict[str, Any]]:
    """Normalize Arabic/fullwidth/kanji values and immediately adjacent units."""

    normalized = unicodedata.normalize("NFKC", text)
    tokens: list[dict[str, Any]] = []
    for match in _NUMBER.finditer(normalized):
        raw = match.group(0)
        value = _kanji_value(raw) if any(char in _KANJI or char in _SMALL or char in _LARGE for char in raw) else _arabic_value(raw)
        if value is None:
            continue
        suffix = _UNIT_SUFFIX.match(normalized, match.end())
        unit = suffix.group(0) if suffix else ""
        dimension, normalized_unit = _UNIT.get(unit, ("number", "number"))
        tokens.append({"value": value, "unit_dimension": dimension, "normalized_unit": normalized_unit})
    return tokens


def _arabic_value(raw: str) -> int | float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _kanji_value(raw: str) -> int | None:
    total = section = digit = 0
    for char in raw:
        if char in _KANJI:
            digit = _KANJI[char]
        elif char in _SMALL:
            section += (digit or 1) * _SMALL[char]
            digit = 0
        elif char in _LARGE:
            total += (section + digit) * _LARGE[char]
            section = digit = 0
        else:
            return None
    return total + section + digit
