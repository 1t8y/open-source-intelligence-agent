"""定义归一化后的采集器数据结构与错误。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

MAX_PER_PAGE: Final[int] = 100
"""两个供应商都接受的最大分页大小，也就是走查可以请求的上限。"""


class CollectorError(RuntimeError):
    """外部采集失败的基类错误。"""


class SourceUnavailableError(CollectorError):
    """当某个源在重试之后仍无法返回有效数据时抛出。"""


@dataclass(frozen=True, slots=True)
class CollectedRepository:
    """供入库服务使用的、与供应商无关的仓库快照。"""

    provider: str
    provider_repo_id: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    license: str | None
    topics: tuple[str, ...]
    stars: int
    forks: int
    observed_at: datetime
    open_issues: int | None = None


@dataclass(frozen=True, slots=True)
class CollectedPage:
    """某个供应商分页搜索结果中的一页。

    `has_more` 采用供应商自己的判断，而不是拿条目数去猜 —— 两者只是
    偶然相等。GitHub 把它写在 `Link` 头里，并在搜索结果触及 1000 条
    上限后不再提供 `rel="next"`；按偏移量分页的供应商则通过返回一个
    不满的页来表达。走查在 `has_more` 为假时停止，所以把边界弄错，
    要么意味着历史被截断，要么意味着多发一次无用请求。
    """

    page: int
    items: tuple[CollectedRepository, ...]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        """返回该页可 JSON 序列化的摘要，不含行本身。"""
        return {"page": self.page, "item_count": len(self.items), "has_more": self.has_more}


class RepositoryCollector(Protocol):
    """每个仓库供应商适配器都要实现的契约。"""

    provider: str
    display_name: str
    base_url: str

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """采集至多指定数量的仓库。"""
        ...


@runtime_checkable
class PaginatedRepositoryCollector(RepositoryCollector, Protocol):
    """可以逐页走查的仓库源。

    `collect` 回答的是「给我 N 个仓库」，这个协议回答的是「给我第 P
    页」。可续跑的走查需要的正是第二个问题：它持久化的游标只是一个
    页码，于是续跑就是用更大的页码再调一次，而不是重放前面的页。

    之所以有 `describe_query`，是因为作业必须记录自己到底查了什么。
    从游标反推只能是猜；问适配器才是唯一知道自己在走哪次搜索的地方，
    而且查询因此只有一份定义，不会出现作业行里存一份、实际发出的请求
    里又一份，然后两份慢慢漂移。
    """

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """返回从 1 开始计数的、至多 `per_page` 个仓库的一页。"""
        ...

    def describe_query(self) -> dict[str, Any]:
        """返回该采集器分页遍历的搜索参数，供作业记录来源。"""
        ...


def clamp_per_page(per_page: int) -> int:
    """拒绝两家供应商都不接受的页大小。"""
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise ValueError(f"per_page must be between 1 and {MAX_PER_PAGE}")
    return per_page


READABLE_CONTENT_TYPES: Final[tuple[str, ...]] = ("readme",)
"""数据源可能返回的内容类型，与 `repository_contents` 的 CHECK 约束保持一致。"""


@dataclass(frozen=True, slots=True)
class FetchedContent:
    """某个供应商的长文本仓库内容，尚未清洗。

    `content` 刻意保持原始形态：清洗与截断属于写入 `repository_contents`
    的唯一那条路径，这样所有调用方对「已缓存」的含义只有一个定义。在这
    里清洗，会让缓存文本及其摘要，与第二个清洗器所产出的结果产生偏差。

    `source_url` 必须是 `https` 链接，因为报告引用会把它带给读者，而
    `reports/citations.py` 会拒绝其它任何形式。
    """

    provider: str
    full_name: str
    content_type: str
    source_url: str
    content: str
    fetched_at: datetime


@runtime_checkable
class RepositoryContentSource(Protocol):
    """同时能交回缓存长文本的仓库源。

    它与 `RepositoryCollector` 分开、而不是并进去，是因为这两项能力由
    不同阶段提出，可用程度也不一样：搜索 API 对每个供应商都能回答「有
    哪些仓库存在」，而「这个仓库的 README 写了什么」各家答法不同，有
    的根本答不了。答不了的供应商干脆不实现它，内容阶段则把这些仓库记
    为不支持，而不是逼它写一个空方法。

    当源*明确回答*没有这份内容时（GitHub 的 `404`），`fetch_readme`
    返回 `None`。抛 `SourceUnavailableError` 只留给「源答不上来」——
    拒绝、超时，或者返回体不是文档写明的形状。这是关于世界的两件不同
    事实，账本也把它们分开记：`missing` 表示仓库没有 README，
    `failed` 表示我们没查清。
    """

    provider: str

    def fetch_readme(self, full_name: str) -> FetchedContent | None:
        """返回仓库 README；源表示没有这份内容时返回 `None`。"""
        ...


def require_page(page: int) -> int:
    """拒绝不是从 1 开始计数的页码。"""
    if page < 1:
        raise ValueError("page must be at least 1")
    return page


def nonnegative_int(value: object, field_name: str) -> int:
    """把供应商给的值转成非负整数，转不了就明确失败。"""
    if isinstance(value, bool):
        converted = int(value)
    elif isinstance(value, int):
        converted = value
    elif isinstance(value, float) and value.is_integer():
        converted = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        converted = int(value)
    else:
        raise SourceUnavailableError(f"invalid integer field: {field_name}")
    if converted < 0:
        raise SourceUnavailableError(f"negative integer field: {field_name}")
    return converted
