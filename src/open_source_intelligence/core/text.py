"""Text helpers shared across layers.

`literal_like` lives here rather than in the module that first needed it, because a second caller
arrived: `pipeline/queries.py` applies it to the repository catalog's `q`, and `topics/queries.py`
applies the same rule to a topic's `name` and `description`. A security-relevant escaping rule is
precisely the thing not to copy into the second caller — two copies stay identical right up until
the day one of them is fixed.

The three cleaning helpers below moved here for the same reason, one layer up: the `contents`
stage writes cached README text and the `get_repository_content` tool reads it back, and both
have to agree on what "cleaned" means. When the writer and the reader each carried their own
copy, the cache could hold text that its own reader would have stripped — and the digest stored
beside it would describe a string no reader ever sees.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: The escape character used when a caller's text is turned into a `LIKE` pattern. The contract
#: calls these filters substring searches, so the characters `LIKE` reserves have to keep meaning
#: themselves: without escaping, `q=_` matches every row that has at least one character and `q=%`
#: matches the entire table, which is the opposite of a search. The backslash is the SQL standard
#: default and SQLAlchemy renders it for both dialects (`ESCAPE '\\'` on MySQL, `ESCAPE '\'` on
#: SQLite).
LIKE_ESCAPE_CHAR: Final[str] = "\\"

#: The characters that have to be escaped to be read literally, `\` first: escaping it after the
#: others would escape the escape characters just inserted.
LIKE_METACHARACTERS: Final[tuple[str, ...]] = (LIKE_ESCAPE_CHAR, "%", "_")

_SCRIPT_RE = re.compile(r"(?is)<script\b.*?</script>")
_STYLE_RE = re.compile(r"(?is)<style\b.*?</style>")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RUN_RE = re.compile(r"[ \t]{3,}")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def literal_like(fragment: str) -> str:
    """Return `fragment` with the `LIKE` metacharacters escaped, so they match themselves.

    The result is a *fragment*: the caller still supplies the surrounding `%` of a substring
    pattern, and must pass `escape=LIKE_ESCAPE_CHAR` to `like()` so the database reads the
    backslashes as escapes rather than as literal backslashes.
    """
    escaped = fragment
    for character in LIKE_METACHARACTERS:
        escaped = escaped.replace(character, f"{LIKE_ESCAPE_CHAR}{character}")
    return escaped


def clean_untrusted_text(raw: str) -> str:
    """Strip script tags, control characters, and runaway whitespace.

    Idempotent by construction, which is what lets the write path and the read path both call
    it: cleaning already-cleaned text returns the same string, so a second pass cannot change
    what the first one stored.
    """
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
