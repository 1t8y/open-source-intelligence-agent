"""主题配置：读侧查询，以及创建、更新、删除操作。

这个包的存在是因为契约要求它存在。`docs/api-contract.md` §3 曾把主题
API 的缺席记为「设计过但没做，谁要加就应该先把契约写进
`INTERFACES.md` §3」，T19 做的就是这件事。被留在外面的，是契约本身
就已论证不该放在这里的那部分：触发一次研究运行不从这里可达，因为一次
运行会花掉模型预算，那个触发器属于调度器与操作者 CLI。
"""

from .errors import TopicError, TopicInUse, TopicNameBlank, TopicNameConflict, TopicNotFound
from .queries import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, TopicPage, clamp_page_size, list_topics
from .service import (
    FIELD_COLUMNS,
    REFERENCE_TABLES,
    create_topic,
    delete_topic,
    normalize_name,
    reference_counts,
    require_usable_name,
    update_topic,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FIELD_COLUMNS",
    "MAX_PAGE_SIZE",
    "REFERENCE_TABLES",
    "TopicError",
    "TopicInUse",
    "TopicNameBlank",
    "TopicNameConflict",
    "TopicNotFound",
    "TopicPage",
    "clamp_page_size",
    "create_topic",
    "delete_topic",
    "list_topics",
    "normalize_name",
    "reference_counts",
    "require_usable_name",
    "update_topic",
]
