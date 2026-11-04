"""跨层共享的文本处理辅助。

`literal_like` 放在这里而不是最初需要它的模块，是因为后来又出现了第二个调用方：
`pipeline/queries.py` 用它处理仓库目录的 `q`，`topics/queries.py` 用同一条规则处理主题的
`name` 与 `description`。一条与安全相关的转义规则，恰恰是最不该被复制进第二个调用方的东西
—— 两份拷贝会一直看起来一致，直到有人只修好了其中一份。

下面三个清洗辅助出于同样的理由上移了一层：`contents` 阶段写入缓存的 README 文本，
`get_repository_content` 工具再把它读出来，写侧和读侧必须对「清洗干净」达成同一个定义。
当写侧和读侧各自持有一份拷贝时，缓存里可能躺着连它自己的读者都会剥掉的文本 —— 而紧挨着
存下的摘要，描述的会是一个任何读者都见不到的字符串。
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: 把调用方文本转成 `LIKE` 模式时使用的转义字符。接口契约把这些筛选器称作「子串搜索」，
#: 因此 `LIKE` 保留的字符必须保持字面含义：不转义时，`q=_` 会匹配所有至少含一个字符的行，
#: `q=%` 会匹配整张表，与「搜索」正好相反。反斜杠是 SQL 标准默认值，SQLAlchemy 会在两种
#: 方言上渲染它（MySQL 上是 `ESCAPE '\\'`，SQLite 上是 `ESCAPE '\'`）。
LIKE_ESCAPE_CHAR: Final[str] = "\\"

#: 需要转义后才能按字面读取的字符，`\` 排在最前：若排在其它字符之后，会把刚刚插入的
#: 转义字符本身再转义一遍。
LIKE_METACHARACTERS: Final[tuple[str, ...]] = (LIKE_ESCAPE_CHAR, "%", "_")

_SCRIPT_RE = re.compile(r"(?is)<script\b.*?</script>")
_STYLE_RE = re.compile(r"(?is)<style\b.*?</style>")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RUN_RE = re.compile(r"[ \t]{3,}")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def literal_like(fragment: str) -> str:
    """转义 `LIKE` 元字符，让 `fragment` 按字面匹配。

    返回值是一个*片段*：调用方仍要自己补上子串模式两侧的 `%`，并且必须把
    `escape=LIKE_ESCAPE_CHAR` 传给 `like()`，数据库才会把反斜杠读成转义符而不是普通字符。
    """
    escaped = fragment
    for character in LIKE_METACHARACTERS:
        escaped = escaped.replace(character, f"{LIKE_ESCAPE_CHAR}{character}")
    return escaped


def clean_untrusted_text(raw: str) -> str:
    """剥掉 script 标签、控制字符和失控的空白。

    构造上即幂等，写侧和读侧才都能安全调用：清洗已经清洗过的文本会得到同一个字符串，
    第二遍不可能改变第一遍存下的内容。
    """
    cleaned = _SCRIPT_RE.sub(" ", raw)
    cleaned = _STYLE_RE.sub(" ", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPACE_RUN_RE.sub("  ", cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """把文本截到 `max_chars`，同时返回「是否发生了截断」。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def content_digest(text: str) -> str:
    """返回给定文本的稳定 SHA-256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
