"""定义报告生成与发布过程中带稳定错误码的失败类型。"""


class ReportGenerationError(ValueError):
    """当草稿无法提升为可追溯报告时抛出。"""

    #: 稳定标识符，让调用方（API、CLI、Worker）可以按失败分支，而不是去匹配消息文本。
    #: 这个字段曾经缺席，调用方只能打印出 `null`。
    code = "REPORT_GENERATION_FAILED"


class ReportNotFoundError(ReportGenerationError):
    """当指定的报告或报告版本不存在时抛出。

    它与通用的生成失败分开，API 才能对缺失的资源回 `404`、对被拒绝的请求回 `400`，
    而不必去匹配消息文本。
    """

    code = "NOT_FOUND"


class ReportPublishError(ValueError):
    """当报告无法到达请求的发布状态时抛出。

    每个子类都带一个稳定的 `code`，被拦下的发布才能记到 `pipeline_runs.error_code`
    上，而不必去匹配消息文本。
    """

    code = "PUBLISH_ERROR"


class ReportStateError(ReportPublishError):
    """当报告当前状态不允许该次迁移时抛出。"""

    code = "REPORT_STATE_INVALID"


class PublishPolicyBlocked(ReportPublishError):
    """当发布将违反发布策略时抛出。

    这就是 `INV-5` 守卫：报告只有在当前版本已有验证结论之后才能发布，而 `MANUAL`
    报告还额外需要一次审批。
    """

    code = "PUBLISH_POLICY_BLOCKED"
