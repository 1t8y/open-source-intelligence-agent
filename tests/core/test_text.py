"""守护共享的 `LIKE` 转义辅助。

`literal_like` 有两个调用方 —— 仓库目录的 `q` 与主题列表的 `q` ——
所以它放在 `core/` 而不是最初需要它的那个模块里。辅助很小，行为却很关键：
正是转义让契约里「子串搜索」这个词成立，而元字符的替换顺序
决定了答案对不对。

这两点都在这里做了断言，并且文件后半段是拿 SQLite 来验证，
而不是看产出的字符串长什么样：一个*看起来*转义过的模式和一个真能按字面
匹配的模式不是同一个命题，而查询模块承诺的只有后者。
末尾的反向对照说明了转义到底防住了什么，
这样前面的通过才是修复的证据，而不是恰好没踩到。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from open_source_intelligence.core.text import LIKE_ESCAPE_CHAR, literal_like


def test_plain_text_is_returned_unchanged() -> None:
    """证明不含元字符的文本不会被改写。"""
    assert literal_like("vector database") == "vector database"


def test_the_escape_character_is_the_sql_standard_backslash() -> None:
    assert LIKE_ESCAPE_CHAR == "\\"


def test_percent_becomes_a_literal_percent() -> None:
    assert literal_like("100%") == "100\\%"


def test_underscore_becomes_a_literal_underscore() -> None:
    assert literal_like("vector_db") == "vector\\_db"


def test_a_real_backslash_is_doubled() -> None:
    """证明调用方自带的反射斜杠也是普通数据，同样要被转义。"""
    assert literal_like("a\\b") == "a\\\\b"


def test_the_backslash_is_escaped_before_the_metacharacters() -> None:
    """顺序才是这个用例的全部意义。

    把 `\\` 放到最后转义，会把本函数刚插在 `_` 前面的那个反斜杠
    再转义一次，`vector_db` 就变成 `vector\\\\_db` —— 一个只匹配
    字面文本 `vector\\_db` 的模式。数一数反斜杠的个数才能区分这两种顺序；
    只跟一个预期字符串比较区分不出来，因为两者看起来都像对的。
    """
    escaped = literal_like("100%")
    assert escaped.count(LIKE_ESCAPE_CHAR) == 1


def _matches(session: Session, value: str, needle: str) -> bool:
    """在测试库上跑一遍查询模块实际拼出的 `%...%`/`ESCAPE` 模式。"""
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
        # 转义真正起作用的那个例子：只有当 `%` 以通配符身份抵达 `LIKE` 时，
        # `100%` 才是 `1000` 的子串。
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
    """把转义所防住的失败测量出来，而不只是描述一遍。

    不转义时，调用方搜一个下划线或百分号字面量，拿回来的是整张表，
    而不是含该字符的那几行 —— 与契约里 `q` 的行为正好相反。
    上面的用例就是对照着它读的：它证明数据库确实把这些字符当通配符，
    因此那些转义后的用例是转义生效的证据，
    而不是恰好因为这个字符本身无害。
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
