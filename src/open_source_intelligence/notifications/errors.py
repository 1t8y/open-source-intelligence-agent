"""报告推送的稳定失败类型。

守卫违规直接抛出；投递失败则记录在 `report_pushes` 行上并作为结果返回。这一分工是刻意的：
「你无权推送这条」是调用方必须修正的请求错误，而「传输层拒收」是一条必须留存为证据、可供
重试的结果。每个类都带有稳定的 `code`，因此 API 可以按问题类型作答，推送行也能在不匹配消息
文本的前提下重试。
"""


class PushError(ValueError):
    """所有报告推送失败的基类。"""

    code = "PUSH_ERROR"


class PushDisabled(PushError):
    """部署已彻底关闭推送时抛出。"""

    code = "PUSH_DISABLED"


class PushNotFound(PushError):
    """寻址的报告或推送记录不存在时抛出。"""

    code = "NOT_FOUND"


class PushBlocked(PushError):
    """报告状态不允许推送时抛出。

    这就是「只有已批准的报告才能推送」背后的守卫：报告必须已通过评审或确实已发布，
    绝不能只是被生成过。
    """

    code = "PUSH_BLOCKED"


class PushTargetNotAllowed(PushError):
    """目的地不是白名单中已启用、已放行的条目时抛出。"""

    code = "PUSH_TARGET_NOT_ALLOWED"


class PushApprovalRequired(PushError):
    """机密报告在待推送版本上缺少人工批准时抛出。"""

    code = "PUSH_APPROVAL_REQUIRED"


class PushDeliveryFailed(PushError):
    """推送根本无法尝试时抛出。

    *传输* 失败不属于这一类：它被记录为一次失败尝试，因此保持可重试。本类覆盖的是不允许
    发起尝试的情形 —— 重试预算已耗尽，或渠道未配置为实时投递。
    """

    code = "PUSH_DELIVERY_FAILED"
