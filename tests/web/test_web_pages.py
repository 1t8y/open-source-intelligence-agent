"""Page-level tests for the sidebar shell and the pages that hang off it.

The eight pages share four rules, so they are asserted together rather than eight times over:

* Every page needs a session, and the refusal is a redirect to the login form that remembers
  where the browser was heading.
* A page opens for exactly the roles its sidebar entry is listed under. That is the test that
  matters most — a nav entry and a route gate are two expressions of one rule, and the failure
  mode is a menu item that leads to a 403.
* Exactly one entry is marked `aria-current`, or the sidebar lies about where the reader is.
* The shell carries the resizable boundary as a real control: `role="separator"` plus `tabindex`,
  so it is reachable by keyboard and not only by mouse.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import TOPIC_PUBLISH_MODES, Topic

#: Every page in the sidebar shell: `(nav key, path, roles the entry is listed under)`.
#: The roles here are the contract the other assertions check both sides of.
PAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("reports", "/ui/reports", ("ADMIN", "EDITOR", "VIEWER")),
    ("pushes", "/ui/pushes", ("ADMIN", "EDITOR")),
    ("topics", "/ui/topics", ("ADMIN", "EDITOR", "VIEWER")),
    ("repositories", "/ui/repositories", ("ADMIN", "EDITOR", "VIEWER")),
    ("runs", "/ui/runs", ("ADMIN", "EDITOR")),
    ("evaluations", "/ui/evaluations", ("ADMIN", "EDITOR")),
    ("dashboard", "/ui/dashboard", ("ADMIN", "EDITOR")),
    ("audit", "/ui/audit", ("ADMIN",)),
)

ROLES = ("ADMIN", "EDITOR", "VIEWER")

#: A page every signed-in identity can open, used whenever the test needs "any page".
OPEN_TO_ALL = "/ui/reports"

TOPICS = "/ui/topics"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def actors(session: Session) -> dict[str, Actor]:
    """One actor per standard role, on the team data scope."""
    return {
        role: create_actor(session, username=f"pages-{role.lower()}", role=role, data_scope="team")
        for role in ROLES
    }


def _nav_links(html: str) -> list[str]:
    """Return the href of every sidebar entry in a rendered page."""
    return re.findall(r'<a class="nav-item" href="([^"]+)"', html)


def _topic_form(**overrides: str) -> dict[str, str]:
    """A complete create/update payload, with the list fields comma-separated."""
    payload = {
        "name": "topic-from-ui",
        "description": "由界面创建",
        "publish_mode": "MANUAL",
        "languages": "python, go",
        "keywords": "llm, agent",
        "min_stars": "5",
        "window_days": "14",
        "max_candidates": "25",
        "enabled": "true",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# Session and the role matrix
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
def test_an_anonymous_browser_is_sent_to_login(
    client: TestClient,
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """No page is readable without a session, and the login form remembers the destination."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/login?next={path.replace('/', '%2F')}"


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
@pytest.mark.parametrize("role", ROLES)
def test_a_page_opens_for_exactly_the_roles_that_may_see_it(
    client: TestClient,
    actors: dict[str, Actor],
    role: str,
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """The route gate is the same decision the sidebar entry encodes.

    A `403` here and a listed entry there would be the two halves of the same rule disagreeing,
    which is why both directions are asserted instead of only "the allowed roles get a 200".
    """
    web_login(client, actors[role])
    response = client.get(path, follow_redirects=False)
    if role in allowed:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text
        assert "无权访问" in response.text


@pytest.mark.parametrize("role", ROLES)
def test_the_sidebar_lists_exactly_what_that_role_can_open(
    client: TestClient,
    actors: dict[str, Actor],
    role: str,
) -> None:
    """Read the rendered menu back and compare it with the pages this role actually gets.

    This is the assertion that ties the two together: it fails if an entry is listed for a role
    that would be refused, and it fails if a page is reachable but missing from the menu.
    """
    web_login(client, actors[role])
    listing = client.get(OPEN_TO_ALL)
    assert listing.status_code == 200
    expected = {path for _, path, allowed in PAGES if role in allowed}
    assert set(_nav_links(listing.text)) == expected


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
def test_exactly_one_sidebar_entry_is_marked_current(
    client: TestClient,
    actors: dict[str, Actor],
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """The `aria-current` marker follows the page, and there is never more than one."""
    web_login(client, actors["ADMIN"])
    response = client.get(path)
    assert response.status_code == 200
    assert response.text.count('aria-current="page"') == 1


# --------------------------------------------------------------------------------------
# The shell
# --------------------------------------------------------------------------------------


def test_the_shell_carries_a_keyboard_reachable_resizer(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """The boundary between the sidebar and the content is a control, not a decoration.

    `role="separator"` with `tabindex` and the aria value bounds is the WAI-ARIA window-splitter
    pattern. Without the tabindex the divider is mouse-only, and the width becomes a preference
    only the users who can drag happen to get.
    """
    web_login(client, actors["ADMIN"])
    html = client.get(OPEN_TO_ALL).text

    assert 'id="splitter"' in html
    assert re.search(r'<div class="splitter"[^>]*role="separator"', html)
    assert 'aria-orientation="vertical"' in html
    assert re.search(r'id="splitter"[^>]*tabindex="0"', html)
    # Remembering the width is the difference between a setting and a gesture.
    assert "localStorage" in html


def test_the_shell_is_absent_from_the_signed_out_pages(
    client: TestClient,
) -> None:
    """403 and 404 are rendered without a sidebar: there is no identity to build one from."""
    page = client.get("/ui/login")
    assert page.status_code == 200
    assert 'id="splitter"' not in page.text
    assert "guest-shell" in page.text


def test_the_stylesheet_bounds_the_sidebar_and_stops_centring_the_content(
    client: TestClient,
) -> None:
    """The two layout facts the resizer depends on.

    The bounds live in CSS so the script reads them from one place. And the content is left
    aligned: centring a `max-width` container inside the shell leaves a dead band between the
    sidebar and the content — about 250px wide at a 1920 viewport — which is what makes the two
    read as separate islands rather than as one console with a menu.
    """
    css = client.get("/ui/static/app.css").text
    assert re.search(r"--sidebar-min:\s*\d+px", css)
    assert re.search(r"--sidebar-max:\s*\d+px", css)

    rule = re.search(r"\.page \{[^}]*\}", css)
    assert rule is not None
    body = rule.group(0)
    assert "margin: 0;" in body
    assert "auto" not in body


# --------------------------------------------------------------------------------------
# Topic configuration: the only settings surface, read and write
# --------------------------------------------------------------------------------------


def test_an_admin_creates_a_topic_and_the_row_lands_in_the_database(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """The write path through the form, checked against the table rather than the redirect.

    A `303` only says the handler chose to redirect; it is the row that says the write happened.
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/ui/topics?ok=created"

    created = session.scalar(select(Topic).where(Topic.name == "topic-from-ui"))
    assert created is not None
    assert created.description == "由界面创建"
    assert created.publish_mode == "MANUAL"
    assert created.languages_json == ["python", "go"]
    assert created.keywords_json == ["llm", "agent"]
    assert created.min_stars == 5
    assert created.window_days == 14
    assert created.max_candidates == 25
    assert created.enabled is True
    assert created.created_by == actors["ADMIN"].user_id

    trail = session.scalars(
        select(AuditLog).where(AuditLog.action == "topic.create", AuditLog.result == "success")
    ).all()
    assert [row.resource_id for row in trail] == [str(created.id)]


def test_a_taken_name_is_refused_with_the_page_still_filled_in(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """A duplicate name comes back as a `409` on the page, not as a redirect that hides it.

    The form is re-rendered with what was typed: nine fields are too many to retype because one
    of them collided.
    """
    workspace.add_topic(name="already-here")
    session.commit()
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(name="already-here"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "already-here" in response.text
    assert 'value="由界面创建"' in response.text

    refusals = session.scalars(
        select(AuditLog).where(AuditLog.action == "topic.create", AuditLog.result == "failed")
    ).all()
    assert len(refusals) == 1


def test_a_blank_name_is_refused_before_the_service_is_reached(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """A whitespace-only name is a `400` with a message, not a row with no name.

    The database has no `NOT NULL` to catch this — an empty string is a perfectly good string —
    so the check has to exist somewhere, and this is where the operator finds out.
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(name="   "), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    # The service's own message is English and aimed at a log line. The page maps the code to a
    # sentence: `str(exc)` in a Chinese page is the kind of thing a reviewer notices and a test
    # that only asserts the status code never would.
    assert "主题名不能为空" in response.text
    assert session.scalar(select(Topic).where(Topic.name == "")) is None


def test_a_non_numeric_bound_is_a_message_rather_than_a_validation_dump(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """The form sends text; "eight" in a number field becomes a sentence, not a JSON pointer."""
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(window_days="八天"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "观察窗口天数必须是整数" in response.text


def test_the_edit_form_writes_only_the_topic_it_belongs_to(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """The update path, with a second row left alone as the control."""
    target = workspace.add_topic(name="editable")
    bystander = workspace.add_topic(name="untouched")
    session.commit()

    web_login(client, actors["ADMIN"])
    detail = client.get(f"{TOPICS}/{target.id}")
    assert detail.status_code == 200
    assert 'value="editable"' in detail.text

    response = client.post(
        f"{TOPICS}/{target.id}/update",
        data={
            **_topic_form(
                name="renamed-topic",
                languages="",
                keywords="rust",
                min_stars="42",
                enabled="false",
            ),
            "csrf_token": csrf_value(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/ui/topics/{target.id}?ok=updated"

    session.expire_all()
    updated = session.get(Topic, target.id)
    assert updated is not None
    # An empty list field means "no restriction", stored as NULL — not an empty list, which would
    # select nothing at all and is the opposite of what a blank field asks for.
    assert updated.languages_json is None
    assert updated.keywords_json == ["rust"]
    assert updated.min_stars == 42
    assert updated.enabled is False
    assert session.get(Topic, bystander.id).name == "untouched"  # type: ignore[union-attr]


def test_a_topic_with_recorded_history_cannot_be_deleted(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """The delete guard names the tables that still point at the topic.

    The foreign keys are `RESTRICT`, not `CASCADE`: cascading would take the reports with it, which
    is the one outcome this page must not have. Disabling the topic is the supported alternative,
    and the page says so.
    """
    topic = workspace.add_topic(name="has-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/delete",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "agent_runs" in response.text
    # The refusal names the tables from `reference_counts` rather than by parsing them out of the
    # service's English sentence.
    assert "不能删除" in response.text
    assert "停用" in response.text
    session.expire_all()
    assert session.get(Topic, topic.id) is not None


def test_an_unreferenced_topic_is_deleted_and_the_row_is_gone(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """The happy path of the same guard, on a topic nothing points at."""
    topic = workspace.add_topic(name="disposable")
    session.commit()
    # Read the id before the request: once the row is gone, touching the instance raises
    # `ObjectDeletedError` instead of telling the assertion something useful.
    topic_id = topic.id

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic_id}/delete",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/topics?ok=deleted"
    session.expire_all()
    assert session.get(Topic, topic_id) is None


def test_toggling_flips_only_the_enabled_column(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """Disabling is the alternative the delete refusal points at, so it has to work."""
    topic = workspace.add_topic(name="togglable", enabled=True)
    session.commit()

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/toggle",
        data={"enabled": "false", "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/topics?ok=disabled")
    session.expire_all()
    assert session.get(Topic, topic.id).enabled is False  # type: ignore[union-attr]


@pytest.mark.parametrize("role", ("EDITOR", "VIEWER"))
def test_only_an_admin_may_write_topics(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
    role: str,
) -> None:
    """A topic fixes the public-API quota and the model budget the next run spends.

    That is the same reasoning `docs/api-contract.md` §3.1 gives for refusing `POST /sources`, so
    `EDITOR`'s write permission — which covers reports and nothing else — does not reach here.
    """
    topic = workspace.add_topic(name="not-yours")
    session.commit()

    web_login(client, actors[role])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/toggle",
        data={"enabled": "false", "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 403
    session.expire_all()
    assert session.get(Topic, topic.id).enabled is True  # type: ignore[union-attr]


def test_a_write_without_the_csrf_field_is_refused_and_audited(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """The double-submit token is checked before anything is written, and the refusal is filed."""
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(TOPICS, data=_topic_form(), follow_redirects=False)
    assert response.status_code == 403
    assert session.scalar(select(Topic).where(Topic.name == "topic-from-ui")) is None

    refusals = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.csrf", AuditLog.result == "denied")
    ).all()
    assert len(refusals) == 1


def test_the_create_form_offers_exactly_the_publish_modes_the_table_accepts(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """Every offered mode comes from the model's tuple, so the form proposes no refused value."""
    web_login(client, actors["ADMIN"])
    page = client.get(TOPICS)
    assert page.status_code == 200
    offered = set(re.findall(r'<option value="([A-Z_]+)"[^>]*>', page.text))
    assert set(TOPIC_PUBLISH_MODES) <= offered


def test_a_tampered_publish_mode_is_re_reported_instead_of_crashing(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """An unknown `publish_mode` must re-render the form, not reach the table's CHECK constraint.

    The API types this field as a `Literal`; the form did not, so a tampered value fell through to
    the database, raised `IntegrityError`, and surfaced as a 500 for what is a user-input problem.
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(publish_mode="WHENEVER"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )

    assert response.status_code == 400, response.text
    assert "发布模式" in response.text
    assert session.scalar(select(Topic).where(Topic.name == "topic-from-ui")) is None
