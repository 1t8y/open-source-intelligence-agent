"""Cleaning and truncation helpers for untrusted external text."""

from __future__ import annotations

import hashlib
import re

_SCRIPT_RE = re.compile(r"(?is)<script\b.*?</script>")
_STYLE_RE = re.compile(r"(?is)<style\b.*?</style>")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RUN_RE = re.compile(r"[ \t]{3,}")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def clean_untrusted_text(raw: str) -> str:
    """Strip script tags, control characters, and runaway whitespace."""
    cleaned = _SCRIPT_RE.sub(" ", raw)
    cleaned = _STYLE_RE.sub(" ", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPACE_RUN_RE.sub("  ", cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Return the text capped at `max_chars` and whether it was cut."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def content_digest(text: str) -> str:
    """Return a stable SHA-256 digest of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
