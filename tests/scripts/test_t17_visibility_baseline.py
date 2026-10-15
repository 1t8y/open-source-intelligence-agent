"""T17's authorization expectations must be measured, not assumed.

The T17 drill seeds four reports and then asserts which of them each account can reach. The
obvious way to write that - compare the API's answer against the ids the drill just created,
spelled as literals (`'1'`, `'1,2,3'`, `'2,4'`) - quietly requires the database's report ids to
begin at 1, which is only true on a volume no one has ever written to.

`scripts/verify_t17.ps1` never drops the volume, and MySQL does not reset `AUTO_INCREMENT` when
rows are deleted, so the *second* run of the drill on the same volume numbers its reports from
wherever the first run left off - the drill's cleanup removes its rows, not its ids. The literals
therefore passed on a fresh volume and failed on a used one, with identical code on both sides,
and the failure looked like an access-control defect when it was an arithmetic one.

`measure_visibility_baseline` measures what the accounts could already read before the drill
writes, and `visibility_expectations` combines that with the ids the drill created. These tests
pin both, and the second one states the old literals outright so the defect cannot come back
silently.
"""

from __future__ import annotations

import pytest
import t17_security_drill
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.security.authorization import (
    VISIBILITY_INTERNAL,
    VISIBILITY_PRIVATE,
    VISIBILITY_TEAM,
)

#: The account the drill's editor stands in for: it owns one of the private reports. High ids keep
#: these rows clear of the ones `Workspace` creates for itself when a topic is seeded.
OWNER_ID = 100
#: Somebody else's id, for the private report the editor must *not* reach.
STRANGER_ID = 200


def _add_user(session: Session, *, user_id: int, username: str) -> None:
    session.add(User(id=user_id, username=username, password_hash="hash", display_name=username))
    session.flush()


def _add_report(
    session: Session,
    *,
    topic_id: int,
    agent_run_id: int,
    title: str,
    visibility: str,
    owner_id: int | None = None,
) -> int:
    """Insert one report with an explicit visibility and owner and return its id."""
    report = Report(
        agent_run_id=agent_run_id,
        topic_id=topic_id,
        owner_id=owner_id,
        title=title,
        current_version=1,
        content_markdown=f"# {title}\n",
        status="published",
        visibility=visibility,
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW,
    )
    session.add(report)
    session.flush()
    return int(report.id)


def _seed_world(workspace: Workspace) -> dict[str, int]:
    """Seed the four pre-existing cases the access rule distinguishes, and return their ids."""
    session = workspace.session
    _add_user(session, user_id=OWNER_ID, username="existing-owner")
    _add_user(session, user_id=STRANGER_ID, username="existing-stranger")
    topic = workspace.add_topic(name="existing-topic")
    run = workspace.add_agent_run(topic.id)
    return {
        "internal": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing internal",
            visibility=VISIBILITY_INTERNAL,
        ),
        "team": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing team",
            visibility=VISIBILITY_TEAM,
        ),
        "owned": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing owned private",
            visibility=VISIBILITY_PRIVATE,
            owner_id=OWNER_ID,
        ),
        "stranger": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing stranger private",
            visibility=VISIBILITY_PRIVATE,
            owner_id=STRANGER_ID,
        ),
    }


def _seed_drill_reports(workspace: Workspace) -> dict[str, int]:
    """Seed the drill's own four reports through the drill's own inserter.

    `seed_reports` builds a topic, an agent run, and an agent config before it gets to the reports;
    the reports are what the expectations are built from, so this seeds those and calls
    `t17_security_drill._seed_report` for each rather than restating the four rows here. If the
    drill changes which report is `INTERNAL`, or who owns which private one, this moves with it.
    """
    session = workspace.session
    topic = workspace.add_topic(name="drill-topic")
    run = workspace.add_agent_run(topic.id)
    return {
        "internal": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["internal"],
            visibility=VISIBILITY_INTERNAL,
            owner_id=None,
        ),
        "team": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["team"],
            visibility=VISIBILITY_TEAM,
            owner_id=None,
        ),
        "private_editor": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["private_editor"],
            visibility=VISIBILITY_PRIVATE,
            owner_id=OWNER_ID,
        ),
        "private_orphan": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["private_orphan"],
            visibility=VISIBILITY_PRIVATE,
            owner_id=None,
        ),
    }


def test_baseline_classifies_pre_existing_reports_by_the_documented_rule(
    session_factory: sessionmaker[Session],
) -> None:
    """The baseline restates `docs/security-and-privacy.md` §3.3 over the columns it is stored in.

    A role's baseline must be the rows it could read *before* the drill wrote: the administrator
    takes everything, the editor adds `TEAM` and its own `PRIVATE` to the shared `INTERNAL`, and
    the viewer holds `INTERNAL` alone. The stranger's private report is what separates a predicate
    that compares the owner from one that only checks that some owner exists.
    """
    with session_factory() as session:
        workspace = Workspace(session)
        existing = _seed_world(workspace)
        session.commit()

        baseline = t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)

    assert baseline.admin.ids == tuple(sorted(existing.values())), "every report"
    assert baseline.admin.total == 4
    assert baseline.editor.ids == tuple(
        sorted((existing["internal"], existing["team"], existing["owned"]))
    ), "the internal, team, and owned private reports"
    assert existing["stranger"] not in baseline.editor.ids, "a private report it does not own"
    assert baseline.editor.total == 3
    assert baseline.viewer.ids == (existing["internal"],), "the internal report alone"
    assert baseline.viewer.total == 1


def test_expectations_are_built_from_the_ids_and_not_from_literals(
    session_factory: sessionmaker[Session],
) -> None:
    """The expectations follow the rows, so a database with history cannot break them.

    The pre-existing reports push the drill's own ids past `1`, which is exactly the state a second
    run of `verify_t17.ps1` leaves behind. The last block states the old literals outright: on this
    database every one of them is false, so a check written against it would report an
    authorization failure that never happened.
    """
    with session_factory() as session:
        workspace = Workspace(session)
        existing = _seed_world(workspace)
        session.commit()

        baseline = t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)
        reports = _seed_drill_reports(workspace)
        session.commit()

        visible, totals = t17_security_drill.visibility_expectations(reports, baseline)

    assert reports["internal"] > max(existing.values()), "the drill's ids continue the table's ids"
    assert visible["admin"] == sorted((*existing.values(), *reports.values())), "all eight reports"
    assert totals["admin"] == 8
    assert visible["editor"] == sorted(
        (
            existing["internal"],
            existing["team"],
            existing["owned"],
            reports["internal"],
            reports["team"],
            reports["private_editor"],
        )
    )
    assert totals["editor"] == 6
    assert visible["viewer"] == sorted((existing["internal"], reports["internal"]))
    assert totals["viewer"] == 2

    assert ",".join(str(value) for value in visible["viewer"]) != "1"
    assert ",".join(str(value) for value in visible["editor"]) != "1,2,3"


def test_baseline_refuses_a_page_that_cannot_hold_the_answer(
    session_factory: sessionmaker[Session],
) -> None:
    """A role already reading more reports than one page holds is refused, not silently truncated.

    The drill lists reports one page at a time, and `GET /api/v1/reports` caps `page_size` at
    `MAX_LIST_PAGE`, so past that ceiling the ids it compares are no longer the whole of what a
    role can read. Comparing a truncated page against a full expectation would fail for reasons
    that have nothing to do with the access rule, so the drill stops - and says which account it
    could not measure and how full the table is.
    """
    with session_factory() as session:
        workspace = Workspace(session)
        topic = workspace.add_topic()
        run = workspace.add_agent_run(topic.id)
        overflow = t17_security_drill.MAX_LIST_PAGE + 1
        # Every row is `INTERNAL`, so every role is over the ceiling and whichever one is measured
        # first is the one named. Which that is is not the point; that it refuses is.
        session.add_all(
            [
                Report(
                    agent_run_id=run.id,
                    topic_id=topic.id,
                    title=f"bulk internal {index}",
                    current_version=1,
                    content_markdown="body",
                    status="published",
                    visibility=VISIBILITY_INTERNAL,
                    sensitivity="INTERNAL",
                    publish_mode="AUTO_WITH_DELAY",
                    generated_at=NOW,
                    published_at=NOW,
                )
                for index in range(overflow)
            ]
        )
        session.commit()

        with pytest.raises(RuntimeError) as raised:
            t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)

    message = str(raised.value)
    assert "admin" in message, "the refusal names the account it could not measure"
    assert str(overflow) in message, "the refusal says how full the table is"


class _StubResponse:
    """The two attributes `_api_readable_ids` reads off a list response."""

    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict[str, object]:
        return {"items": [{"id": 7}], "total": 1}


class _RecordingClient:
    """A `TestClient` stand-in that records the URL it was asked for."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _StubResponse:
        self.urls.append(url)
        return _StubResponse()


def test_the_drill_lists_a_full_page() -> None:
    """The ceiling the baseline measures against is the page the drill actually requests.

    `measure_visibility_baseline` refuses once a role can read more rows than the list response
    carries. That ceiling is only the right one if the drill asks for a full page: asking for fewer
    would make the refusal fire only after the answer had stopped fitting in one response, and
    asking for more is a `422` from the endpoint's own bound.
    """
    client = _RecordingClient()

    ids, total = t17_security_drill._api_readable_ids(client, {})  # type: ignore[arg-type]

    assert ids == [7]
    assert total == 1
    assert f"page_size={t17_security_drill.MAX_LIST_PAGE}" in client.urls[0]
