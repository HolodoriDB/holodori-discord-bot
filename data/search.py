"""fuzzy title/name matching (rapidfuzz), for autocomplete, command resolution, and guess answers.

no romanization stack (cutlet/korean/pinyin) - holodori titles come through the api already
romanized for the chosen language. autocomplete blends token_set + WRatio for recall; single-answer
matching uses length-aware ratio with a sensitivity threshold to avoid false positives.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable, TypeVar

from rapidfuzz import fuzz

T = TypeVar("T")

_APOSTROPHES = "'’‘`´"
AUTOCOMPLETE_FLOOR = 60
DEFAULT_SENSITIVITY = 80  # min length-aware ratio for a single-answer match


def preprocess(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    return re.sub(r"\s+", " ", text)


def fold(text: str) -> str:
    """drop punctuation so it can't hide a word from the tokenizer (apostrophes close up)."""
    out: list[str] = []
    for ch in preprocess(text):
        if ch in _APOSTROPHES:
            continue
        out.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def rank(
    query: str, items: Iterable[T], keyfn: Callable[[T], Iterable[str | None]], limit: int = 25
) -> list[T]:
    q = fold(query)
    items = list(items)
    if not q:
        return items[:limit]
    scored: list[tuple[float, int, T]] = []
    for i, it in enumerate(items):
        keys = [fold(k) for k in keyfn(it) if k]
        if not keys:
            continue
        score = max(fuzz.WRatio(q, k) for k in keys)
        if any(k == q for k in keys):
            score += 1000
        elif any(k.startswith(q) for k in keys):
            score += 200
        elif any(q in k for k in keys):
            score += 50
        if score >= AUTOCOMPLETE_FLOOR:
            scored.append((score, i, it))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [it for _, _, it in scored][:limit]


def best_match(
    query: str,
    items: Iterable[T],
    keyfn: Callable[[T], Iterable[str | None]],
    *,
    sensitivity: float = DEFAULT_SENSITIVITY,
) -> T | None:
    q = fold(query)
    if not q:
        return None
    best: T | None = None
    best_score = 0.0
    for it in items:
        for raw in keyfn(it):
            if not raw:
                continue
            k = fold(raw)
            if not k:
                continue
            if k == q:
                return it
            s = float(fuzz.ratio(q, k))
            if len(q) >= 3 and k.startswith(q):
                s = max(s, 92.0)
            if s > best_score:
                best_score, best = s, it
    return best if best_score >= sensitivity else None
