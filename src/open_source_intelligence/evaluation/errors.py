"""为评测层定义稳定的错误码。

`api/errors.py` 把它们映射成 Problem Details 的状态码，而 CLI 打印的是 `code` 而不是消息
文本，于是脚本可以按失败类型分支，不必解析英文。`NOT_FOUND` 由所有「行不存在」的场景共用，
因为 API 把它们一律渲染成 `404`，而资源名本来就写在消息里。
"""

from __future__ import annotations


class EvaluationError(Exception):
    """承载稳定错误码的所有评测失败的基类。"""

    code: str = "EVALUATION_ERROR"


class DatasetInvalid(EvaluationError):
    """数据集文件格式错误、内容不完整或内部自相矛盾。"""

    code = "DATASET_INVALID"


class DatasetVersionConflict(EvaluationError):
    """同一数据集版本已存在，但内容不同。

    版本不可变，因此这里选择拒绝而不是合并：在既有版本号下静默接受一次改动，会让每一份
    历史结果都无法复现。
    """

    code = "DATASET_VERSION_CONFLICT"


class DatasetTopicMissing(EvaluationError):
    """某个启用中的用例引用了本部署没有的主题。

    照常导入，得到的数据集里每个变体都会以 `EVAL_TOPIC_NOT_FOUND` 失败 —— 五行绿色的
    `failed` 看起来像模型出了问题，实际上是数据集绑到了别人的主题名上。检查放在导入时，
    因为那是运维正盯着这份数据集、也唯一能把它修好的时刻。
    """

    code = "DATASET_TOPIC_MISSING"


class EvaluationNotFound(EvaluationError):
    """数据集、用例或运行不存在。"""

    code = "NOT_FOUND"


class UnknownTarget(EvaluationError):
    """请求的目标变体不在已定义的那五个变体之内。"""

    code = "UNKNOWN_TARGET"


class EvaluationDisabled(EvaluationError):
    """评测已被配置关闭。"""

    code = "EVALUATION_DISABLED"
