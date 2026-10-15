"""The drills' cleanups must delete only rows they can prove are their own.

Every smoke labels the rows it creates with a name prefix (`__t7_smoke_`,
`__T15 report__`, `__t17_drill_` ...) and later deletes "its" rows by that prefix. The obvious
spelling, `Column.like(f"{prefix}%")`, does not mean "begins with this prefix": `_` is a
single-character wildcard in `LIKE`, and every one of these prefixes contains one, so the pattern
also matches values the drill never wrote. A cleanup that can delete a stranger's row is worse
than one that leaves its own behind, so each drill matches a **literal** prefix now
(`startswith(..., autoescape=True)`).

These tests drive the drills' own cleanup functions - not a copy of their predicates - against a
database holding two rows: one labelled the way the drill labels its own, and one that only the
`LIKE` pattern would have matched. The first must be removed, the second must survive. Against the
old `like(f"{prefix}%")` spelling every case below fails, which is the point: a row left behind is
a mess, a row deleted by mistake is a defect.

The cases cover the thirteen places a drill *deletes* by a prefix. The remaining sites that match
a prefix are counts (`t16._repo_count`, the injection and repeat counters in `t17`), and they are
left out on purpose: an over-broad count does not delete anything, it makes the drill's own
assertion disagree and fail loudly, whereas an over-broad delete is silent and destroys a row that
belongs to someone else. Both spell the match the same way, so the escaping these cases pin is the
escaping they use.

The scripts are importable because `pyproject.toml` puts `scripts/` on the pytest `pythonpath`;
several of them already import each other by bare module name.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
import t7_mysql_smoke
import t8_mysql_smoke
import t11_mysql_smoke
import t13_mysql_smoke
import t14_mysql_smoke
import t15_mysql_smoke
import t16_mysql_smoke
import t17_security_drill
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.agent import AgentConfig
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Repository
from open_source_intelligence.persistence.models.notifications import PushTarget
from open_source_intelligence.persistence.models.reporting import Report

# --------------------------------------------------------------------------------------
# One seeding function and one reader per label column the drills clean up by
# --------------------------------------------------------------------------------------


def _seed_repository(workspace: Workspace, labels: Sequence[str]) -> None:
    source = workspace.add_source()
    for label in labels:
        workspace.add_repository(
            source_id=source.id, provider_repo_id=label, full_name=f"acme/{label}"
        )


def _repositories(session: Session) -> set[str]:
    return set(session.scalars(select(Repository.provider_repo_id)))


def _seed_report(workspace: Workspace, labels: Sequence[str]) -> None:
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    for label in labels:
        workspace.add_report(topic_id=topic.id, agent_run_id=run.id, title=label)


def _reports(session: Session) -> set[str]:
    return set(session.scalars(select(Report.title)))


def _seed_user(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.session.add(User(username=label, password_hash="hash", display_name=label))
    workspace.session.flush()


def _users(session: Session) -> set[str]:
    return set(session.scalars(select(User.username)))


def _seed_agent_config(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.session.add(
            AgentConfig(
                name=label,
                version="1",
                provider="offline",
                model="offline",
                prompt_version="v1",
                tool_policy_json={},
            )
        )
    workspace.session.flush()


def _agent_configs(session: Session) -> set[str]:
    return set(session.scalars(select(AgentConfig.name)))


def _seed_push_target(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.add_push_target(name=label)


def _push_targets(session: Session) -> set[str]:
    return set(session.scalars(select(PushTarget.name)))


def _seed_t15_reports(workspace: Workspace, labels: Sequence[str]) -> None:
    """Seed reports the *only* way the T15 cleanup can reach is its title predicate.

    T15's title delete sits inside `if topic_ids:`, where the ids come from its own topic name, so
    that topic has to exist or the predicate never runs. The reports are nevertheless filed under
    a different topic: put them under the drill's topic and the `topic_id IN (...)` delete would
    remove them anyway, and this case would pass without the title predicate being correct at all.
    """
    workspace.add_topic(name=t15_mysql_smoke.TOPIC_NAME)
    carrying = workspace.add_topic(name="t15-unrelated-topic")
    run = workspace.add_agent_run(carrying.id)
    for label in labels:
        workspace.add_report(topic_id=carrying.id, agent_run_id=run.id, title=label)


# --------------------------------------------------------------------------------------
# The cleanups that take an actor map; an empty one is the no-actor path
# --------------------------------------------------------------------------------------


def _t13_cleanup(session: Session) -> None:
    t13_mysql_smoke._cleanup(session, {})


def _t14_cleanup(session: Session) -> None:
    t14_mysql_smoke._cleanup(session, {})


def _t15_cleanup(session: Session) -> None:
    t15_mysql_smoke._cleanup(session, {})


# --------------------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drill:
    """One cleanup pass, the prefix it owns, and a value only `LIKE` would have matched."""

    name: str
    prefix: str
    adversary: str
    cleanup: Callable[[Session], None]
    seed: Callable[[Workspace, Sequence[str]], None]
    labels: Callable[[Session], set[str]]


#: Every `adversary` below was checked against the drill's old pattern with `SELECT ? LIKE ?`; each
#: one satisfies `LIKE f"{prefix}%"` while not starting with `prefix`.
CASES = (
    Drill(
        name="t7-repositories",
        prefix="__t7_smoke_",
        adversary="aat7xsmokey1",
        cleanup=t7_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t7-agent-configs",
        prefix="__t7_smoke_",
        adversary="aat7xsmokey1",
        cleanup=t7_mysql_smoke.cleanup_verification_records,
        seed=_seed_agent_config,
        labels=_agent_configs,
    ),
    Drill(
        name="t8-repositories",
        prefix="__t8_smoke_",
        adversary="aat8xsmokey1",
        cleanup=t8_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t11-reports",
        prefix="__T11",
        adversary="abT11foo",
        cleanup=t11_mysql_smoke._cleanup,
        seed=_seed_report,
        labels=_reports,
    ),
    Drill(
        name="t13-push-targets",
        prefix="__T13",
        adversary="abT13foo",
        cleanup=_t13_cleanup,
        seed=_seed_push_target,
        labels=_push_targets,
    ),
    Drill(
        name="t14-repositories",
        prefix="__t14_smoke_",
        adversary="aat14xsmokey1",
        cleanup=_t14_cleanup,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t15-reports",
        prefix="__T15 report__",
        adversary="abT15 reportzz",
        cleanup=_t15_cleanup,
        seed=_seed_t15_reports,
        labels=_reports,
    ),
    Drill(
        name="t15-repositories",
        prefix="__t15_smoke_",
        adversary="aat15xsmokey1",
        cleanup=_t15_cleanup,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t16-repositories",
        prefix="__t16_smoke_",
        adversary="aat16xsmokey1",
        cleanup=t16_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t17-reports",
        prefix="__T17",
        adversary="abT17foo",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_report,
        labels=_reports,
    ),
    Drill(
        name="t17-users",
        prefix="__t17_",
        adversary="abt17c_smith",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_user,
        labels=_users,
    ),
    Drill(
        name="t17-push-targets",
        prefix="__t17_",
        adversary="abt17xtarget",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_push_target,
        labels=_push_targets,
    ),
    Drill(
        name="t17-repositories",
        prefix="__t17_drill_",
        adversary="aat17xdrilly1",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_repository,
        labels=_repositories,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_cleanup_removes_its_own_rows_and_leaves_the_rest(
    case: Drill, session_factory: sessionmaker[Session]
) -> None:
    """The drill's own row is deleted; the value that only `LIKE` matched survives.

    Both rows carry a plausible label. The first is `{prefix}genuine`, which is how every drill
    names what it creates and which any prefix match finds. The second is a value that satisfies
    the old `LIKE` pattern without starting with the prefix - the row a drill has no claim on.
    """
    own = f"{case.prefix}genuine"
    with session_factory() as session:
        workspace = Workspace(session)
        case.seed(workspace, (own, case.adversary))
        session.commit()

        case.cleanup(session)

        remaining = case.labels(session)
        assert own not in remaining, "the cleanup left a row of its own behind"
        assert case.adversary in remaining, "the cleanup deleted a row the drill never wrote"
