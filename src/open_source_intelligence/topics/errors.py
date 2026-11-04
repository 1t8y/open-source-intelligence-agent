"""主题配置中稳定的失败类型。

每个类都带一个稳定的 `code`，`api/errors.py` 才能渲染出 Problem
Details 响应体，调用方也可以按 code 分支，而不是去解析英文。这三种失
败确实是不同的答案：名字被占用，是调用方换一个名字就能解决的；被历史
引用到的主题根本不能删除；而不存在的主题没有答案可给。
"""


class TopicError(ValueError):
    """所有主题配置失败类型的基类。"""

    code = "TOPIC_ERROR"


class TopicNotFound(TopicError):
    """当被指向的主题不存在时抛出。

    只有直接调用方的路径会抛它。API 通过
    `security/authorization.py:authorize_topic_read` 读主题，那里抛自己
    的 `404`，这样存在性检查与可见性检查就无法被拆开。
    """

    code = "NOT_FOUND"


class TopicNameBlank(TopicError):
    """当主题名去掉首尾空白后为空时抛出。

    基类选 `ValueError` 是有意的。HTTP 边界用一个 Pydantic
    `AfterValidator` 校验 `name` 字段，它调用
    `service.require_usable_name`，而 Pydantic 会把 `ValueError` 变成
    指名该字段的 `422`；同一个函数也守着写入本身，所以脚本或运维界面得
    到的是完全相同的拒绝，而不是一行没有名字的记录。一次检查，两个出
    口。
    """

    code = "TOPIC_NAME_BLANK"


class TopicNameConflict(TopicError):
    """当另一个主题已经带着请求的名字时抛出。

    唯一性由数据库承担。这个类把那条约束变成一个具名冲突，而不是一个
    `IntegrityError`：两个调用方同时抢同一个名字，双方都必须得到一个可
    据以行动的答复，而违反约束产生的 500 不是。
    """

    code = "TOPIC_NAME_CONFLICT"


class TopicInUse(TopicError):
    """当主题因被已记录的历史引用而无法删除时抛出。

    `agent_runs`、`pipeline_runs`、`reports` 与 `report_drafts` 都以
    `ON DELETE RESTRICT` 指向 `topics.id`，它们记录的是这个主题产出过
    什么。消息里点名持有引用的表，因为只说「正在被使用」而不说在哪里，
    等于让调用方在四张表里猜。
    """

    code = "TOPIC_IN_USE"
