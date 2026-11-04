"""JSON API 共用的请求参数类型。

有一条规则必须在这里说一次：**时间戳查询参数必须带时区偏移。**

`persistence/base.py:UTCDateTime` 拒绝去猜一个不带时区的时间戳原本属于哪个时区，这是对的。
把 `2026-09-24T00:00:00` 读成 UTC 是一种无声的假设；读成本地时间则会让同一个 URL 在不同部署
上含义不同。两者都比直接说不更糟。

这个类型改变的是*拒绝发生的位置*。没有它时，`ValueError` 会从查询构造器内部冒出来变成
`500`，既没告诉调用方任何信息，看起来还像一次故障。有了它，同样的输入会在边界上被拒绝成
`422` 并点名字段，这正是 `docs/api-contract.md` §9 对非法参数的承诺。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

_OFFSET_HINT = "timestamp must include a timezone offset, e.g. 2026-09-24T00:00:00Z"


def _require_timezone_offset(value: datetime) -> datetime:
    """拒绝没有声明自己处在哪个时区的时间戳。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(_OFFSET_HINT)
    return value


#: 保证在进入查询之前已经带上时区信息的 `datetime`。
UtcDateTime = Annotated[datetime, AfterValidator(_require_timezone_offset)]
