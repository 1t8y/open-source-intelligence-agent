"""Push-channel stubs and report seeding shared by the notification, API, and web tests.

Like `reports_support.py` this sits at the top of `tests/` so every test directory can import
it as a plain top-level module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reports_support import build_report
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.notifications.channels import DeliveryReceipt, OutboundMessage
from open_source_intelligence.persistence.models.reporting import Report

#: The two statuses a report may be pushed from, spelled out so tests read like the rule.
APPROVED = "approved"
PUBLISHED = "published"


@dataclass(slots=True)
class StubChannel:
    """Deterministic channel that records what it was asked to send.

    `fail_times` makes the first N attempts fail, which is how the retry tests build a transport
    that starts broken and then recovers without touching the network.
    """

    channel_name: str = "email"
    fail_times: int = 0
    detail: str = "stub"
    sent: list[tuple[str, OutboundMessage]] = field(default_factory=list)
    attempts: int = 0

    @property
    def channel(self) -> str:
        """Return the channel this stub stands in for."""
        return self.channel_name

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """Record the attempt and fail the first `fail_times` of them."""
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
    """Generate a report and arrange it into one of the statuses a push is allowed from.

    The status is assigned directly because these tests are about the *push* guards, not about
    the publishing state machine: the pushable statuses are reached through the publish workflow
    elsewhere, and re-running it here would only make the tests slower and less focused.
    """
    case = build_report(session, workspace, publish_mode="MANUAL")
    report = case.reload(session)
    report.status = status
    report.visibility = visibility
    report.sensitivity = sensitivity
    session.commit()
    return report
