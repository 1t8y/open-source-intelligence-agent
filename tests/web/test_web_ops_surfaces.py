"""Who sees an operations-configuration surface, and who is merely told to ask.

Two things live behind this rule. A **push target** is deployment configuration — it fixes which
destinations a published report may leave through — and **an account** is created by the bootstrap
CLI. Neither is managed from the console; the backend owns them. So a page that lists the allowlist
is drawn for an admin, who can act on it, and a page that merely has to say "you cannot push yet"
says something different depending on whether the reader could act.

The other half is that a rendering rule is not a gate. Hiding a button from a role does not stop
that role from posting it, so each surface here is checked twice: what the page draws, and what the
route answers. A test that only checked the first would pass on an interface that lies, and a test
that only checked the second would pass on one that offers a button nobody without the role can use.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

#: The page that carries both the delivery ledger and the allowlist card.
PUSHES = "/ui/pushes"
REPORTS = "/ui/reports"
RELEASE_ACTION = 'action="/ui/reports/publish-due"'

#: The table the allowlist lives in. It is the backend's name for the thing, and a reader who
#: cannot act on it should not be handed it: a page telling an editor to go edit `push_targets`
#: is a page instructing them to do something they have no way to do.
ALLOWLIST_TABLE = "push_targets"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def actors(session: Session) -> dict[str, Actor]:
    """One actor per standard role, on the team data scope."""
    return {
        role: create_actor(session, username=f"ops-{role.lower()}", role=role, data_scope="team")
        for role in ("ADMIN", "EDITOR", "VIEWER")
    }


# --------------------------------------------------------------------------------------
# The allowlist card
# --------------------------------------------------------------------------------------


def test_an_admin_reads_the_allowlist_on_the_delivery_page(
    workspace: Workspace,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """The card shows the whole allowlist, including the two switches that widen delivery."""
    target = workspace.add_push_target(name="ops-email")

    assert web_login(client, actors["ADMIN"]).status_code == 303
    page = client.get(PUSHES)

    assert page.status_code == 200
    assert "推送目标白名单" in page.text
    assert "允许机密" in page.text
    assert target.name in page.text


def test_an_editor_gets_the_delivery_ledger_without_the_allowlist(
    workspace: Workspace,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """An editor may read what was delivered, not the configuration that decides where it can go.

    The ledger answers "did the report I pushed arrive"; the allowlist answers "what may this
    platform send out and under which switches". Those are different questions, and only the first
    one is an editor's business — so the card, its columns, its destinations and the backend's own
    name for the table are all absent.
    """
    target = workspace.add_push_target(name="ops-email")

    assert web_login(client, actors["EDITOR"]).status_code == 303
    page = client.get(PUSHES)

    assert page.status_code == 200
    assert "投递记录" in page.text
    assert "推送目标白名单" not in page.text
    assert "允许机密" not in page.text
    assert ALLOWLIST_TABLE not in page.text
    assert target.name not in page.text


def test_a_viewer_cannot_open_the_delivery_page_at_all(
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """The card's visibility is a second gate; the page's own gate is unchanged."""
    assert web_login(client, actors["VIEWER"]).status_code == 303
    assert client.get(PUSHES).status_code == 403


# --------------------------------------------------------------------------------------
# The release driver on the report list
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("role", "drawn"), [("ADMIN", True), ("EDITOR", True), ("VIEWER", False)])
def test_the_release_driver_is_drawn_only_for_a_role_that_can_run_it(
    role: str,
    drawn: bool,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """The button follows the route's own role gate — it used to be offered to readers too.

    A viewer could see the button and only find out on the click that the route refuses it. That is
    the same defect as a sidebar entry that opens to a 403, and the same fix: render the control
    under the rule the endpoint enforces.
    """
    assert web_login(client, actors[role]).status_code == 303
    page = client.get(REPORTS)

    assert page.status_code == 200
    assert (RELEASE_ACTION in page.text) is drawn


def test_hiding_the_button_is_not_what_stops_a_read_only_role(
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """The absent button is a courtesy, not the gate: the POST is refused on its own.

    Both halves are needed. Without the route check the interface's rule would be the only rule,
    and a rendering change would silently become an authorization change.
    """
    assert web_login(client, actors["VIEWER"]).status_code == 303
    client.get(REPORTS)

    response = client.post("/ui/reports/publish-due", data={"csrf_token": csrf_value(client)})

    assert response.status_code == 403
