"""Tests for the shared `LIKE` escaping helper.

`literal_like` has two callers - the repository catalog's `q` and the topic list's `q` - which is
why it lives in `core/` instead of in the module that needed it first. Small helper, load-bearing
behaviour: the escaping is what makes the contract's word "substring search" true, and the order
the metacharacters are replaced in decides whether the answer is right.

Both are asserted here, and the second half of the file asserts them against SQLite rather than
against the shape of the produced string: a pattern that *looks* escaped and a pattern that
matches literally are not the same claim, and only the second one is the promise the query modules
make. The negative control at the end states what the escaping prevents, so the passing cases are
evidence of a fix rather than of an accident.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from open_source_intelligence.core.text import LIKE_ESCAPE_CHAR, literal_like


def test_plain_text_is_returned_unchanged() -> None:
    """Text without metacharacters is not rewritten."""
    assert literal_like("vector database") == "vector database"


def test_the_escape_character_is_the_sql_standard_backslash() -> None:
    assert LIKE_ESCAPE_CHAR == "\\"


def test_percent_becomes_a_literal_percent() -> None:
    assert literal_like("100%") == "100\\%"


def test_underscore_becomes_a_literal_underscore() -> None:
    assert literal_like("vector_db") == "vector\\_db"


def test_a_real_backslash_is_doubled() -> None:
    """A caller's own backslash is data like any other character, so it is escaped too."""
    assert literal_like("a\\b") == "a\\\\b"


def test_the_backslash_is_escaped_before_the_metacharacters() -> None:
    """Order is the whole point of this test.

    Escaping `\\` last would take the backslash this function just inserted before `_` and escape
    *that*, turning `vector_db` into `vector\\\\_db` - a pattern that matches the literal text
    `vector\\_db` and nothing else. Counting the backslashes is what distinguishes the two orders;
    comparing against a single expected string would not, because both are plausible-looking.
    """
    escaped = literal_like("100%")
    assert escaped.count(LIKE_ESCAPE_CHAR) == 1


def _matches(session: Session, value: str, needle: str) -> bool:
    """Run the `%...%`/`ESCAPE` pattern the query modules build, against the test database."""
    pattern = f"%{literal_like(needle)}%"
    return bool(
        session.scalar(
            text("SELECT :value LIKE :pattern ESCAPE :escape"),
            {"value": value, "pattern": pattern, "escape": LIKE_ESCAPE_CHAR},
        )
    )


@pytest.mark.parametrize(
    ("value", "needle", "expected"),
    [
        ("cut 65% of tokens", "%", True),
        ("cut 65 tokens", "%", False),
        # The case that makes the escaping matter: `100%` is a substring of `1000` only if `%`
        # reaches `LIKE` as a wildcard.
        ("1000", "100%", False),
        ("100%", "100%", True),
        ("timm/mobilenetv3_small_100", "_", True),
        ("acme/rocket", "_", False),
        ("vector database", "vector", True),
        ("vector database", "VECTOR", True),
    ],
)
def test_the_pattern_matches_the_needle_literally(
    session: Session,
    value: str,
    needle: str,
    expected: bool,
) -> None:
    assert _matches(session, value, needle) is expected


@pytest.mark.parametrize(
    ("raw_pattern", "escaped_pattern"),
    [
        ("%_%", "%\\_%"),
        ("%%%", "%\\%%"),
    ],
)
def test_an_unescaped_metacharacter_matches_everything(
    session: Session,
    raw_pattern: str,
    escaped_pattern: str,
) -> None:
    """The failure the escaping prevents, measured rather than described.

    Without the escape, a caller searching for a literal underscore or percent sign gets back the
    whole table instead of the rows containing that character - the opposite of what the contract
    says `q` does. This is the control the cases above are read against: it shows the database
    really does treat those characters as wildcards, so the escaped cases are evidence of the
    escape working and not of the character happening to be inert.

    Both patterns are run against one value that contains neither character, which is the value the
    escaped form must reject.
    """
    probe = "SELECT 'acme/rocket' LIKE :pattern ESCAPE :escape"
    assert session.scalar(text("SELECT 'acme/rocket' LIKE :pattern"), {"pattern": raw_pattern}) == 1
    assert (
        session.scalar(
            text(probe),
            {"pattern": escaped_pattern, "escape": LIKE_ESCAPE_CHAR},
        )
        == 0
    )
