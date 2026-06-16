from __future__ import annotations

import re

# Typical mojibake markers when UTF-8 Cyrillic was decoded with a wrong encoding.
_BAD_MARKERS = ("Р", "С", "Ð", "Ñ", "â", "袙", "褋", "邪")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def _quality_score(text: str) -> int:
    cyr = len(_CYR_RE.findall(text))
    bad = sum(text.count(marker) for marker in _BAD_MARKERS)
    return (cyr * 3) - (bad * 4)


def _redecode(text: str, source_encoding: str) -> str | None:
    try:
        return text.encode(source_encoding).decode("utf-8")
    except Exception:
        return None


def normalize_text(text: str) -> str:
    """
    Best-effort anti-mojibake normalization for Telegram texts.
    If source text is already fine, returns it unchanged.
    """
    if not text:
        return text

    best = text
    best_score = _quality_score(text)
    for source_enc in ("cp1251", "latin1", "cp866"):
        candidate = _redecode(text, source_enc)
        if not candidate:
            continue
        score = _quality_score(candidate)
        if score > best_score:
            best = candidate
            best_score = score
    return best

