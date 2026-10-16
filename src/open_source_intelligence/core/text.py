"""Text helpers shared by the read-side query modules.

`literal_like` lives here rather than in the module that first needed it, because a second caller
arrived: `pipeline/queries.py` applies it to the repository catalog's `q`, and `topics/queries.py`
applies the same rule to a topic's `name` and `description`. A security-relevant escaping rule is
precisely the thing not to copy into the second caller — two copies stay identical right up until
the day one of them is fixed.
"""

from __future__ import annotations

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
