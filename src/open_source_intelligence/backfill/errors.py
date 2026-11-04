"""可续跑回填的领域错误。"""


class BackfillError(RuntimeError):
    """回填无法继续或未能按时完成时的基类错误。"""


class BackfillJobNotFound(BackfillError):
    """当作业 id 不存在时抛出。"""


class BackfillLimitError(BackfillError):
    """当作业请求超出 schema 接受的边界时抛出。"""


class BackfillNotResumable(BackfillError):
    """当作业已经没有可走查的页时抛出。

    在数据库意义上这不算错误条件 —— 已完成的作业正是成功的情形 —— 但续
    跑一个这样的作业，要么会重放游标已经越过的页，要么会谎称作业取得了
    它从未取得的进展，所以走查选择拒绝，而不是去猜调用方想要哪一种。
    """


class BackfillUnsupportedCollector(BackfillError):
    """当选中的源无法分页时抛出。"""


class BackfillWalkFailed(BackfillError):
    """在一次走查已经记录了自己的失败之后抛出。

    抛出时，作业行与它的运行行都已经描述了发生了什么；这里附带上下文，
    是为了让调用方不必重读数据库就能报告游标停在哪里。关键字参数默认取
    空值，是为了让 Celery 能从只带消息的 JSON 结果后端重建这个错误。
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: int = 0,
        run_id: int = 0,
        error_code: str = "",
        next_page: int = 0,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.run_id = run_id
        self.error_code = error_code
        self.next_page = next_page


class BackfillJobBusy(BackfillError):
    """当另一个走查仍持有该作业的游标时抛出。"""

    def __init__(self, message: str, *, job_id: int, held_since: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.held_since = held_since
