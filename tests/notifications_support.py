"""通知、API 与 Web 测试共用的推送渠道替身与报告种子数据。

和 `reports_support.py` 一样，这个文件放在 `tests/` 顶层，
各测试目录才能把它当作普通的顶层模块直接导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reports_support import build_report
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.notifications.channels import DeliveryReceipt, OutboundMessage
from open_source_intelligence.persistence.models.reporting import Report

#: 报告允许被推送的两种状态，单独列出来，让测试读起来就像规则本身。
APPROVED = "approved"
PUBLISHED = "published"


@dataclass(slots=True)
class StubChannel:
    """记录每一次发送请求的确定性渠道替身。

    `fail_times` 会让前 N 次尝试失败，重试测试正是靠它造出一个
    「先坏后自愈」的传输层，全程不碰网络。
    """

    channel_name: str = "email"
    fail_times: int = 0
    detail: str = "stub"
    sent: list[tuple[str, OutboundMessage]] = field(default_factory=list)
    attempts: int = 0

    @property
    def channel(self) -> str:
        """返回这个替身所代理的渠道名。"""
        return self.channel_name

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """记录本次尝试，并让前 `fail_times` 次尝试失败。"""
        self.attempts += 1
        self.sent.append((destination, message))
        if self.attempts <= self.fail_times:
            return DeliveryReceipt(ok=False, detail=f"{self.detail}:transient")
        return DeliveryReceipt(ok=True, detail=f"{self.detail}:ok")


def seed_pushable_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = APPROVED,
    visibility: str = "INTERNAL",
    sensitivity: str = "INTERNAL",
) -> Report:
    """生成一份报告，并把它摆到允许推送的某个状态上。

    状态直接赋值，是因为这些用例考察的是*推送*侧的守卫，而不是发布
    状态机：可推送状态在别处由发布流程走到，这里再跑一遍流程只会让
    测试更慢、焦点更散。
    """
    case = build_report(session, workspace, publish_mode="MANUAL")
    report = case.reload(session)
    report.status = status
    report.visibility = visibility
    report.sensitivity = sensitivity
    session.commit()
    return report
