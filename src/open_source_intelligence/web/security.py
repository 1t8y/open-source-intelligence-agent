"""Cookie session and CSRF protection for the server-rendered admin UI.

The UI authenticates with the same JWT the API issues but keeps it in an `HttpOnly` cookie,
so no script can read it (`docs/security-and-privacy.md` §3.1). Every mutating form
additionally carries a double-submit CSRF token: a random value lives in a cookie and is
echoed in a hidden field, and the two must match byte for byte.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Request
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.service import get_user_by_id
from open_source_intelligence.security.tokens import decode_access_token

SESSION_COOKIE = "osia_session"
CSRF_COOKIE = "osia_csrf"
CSRF_FIELD = "csrf_token"
LOGIN_PATH = "/ui/login"


class WebRedirect(Exception):
    """Raised by UI dependencies to send the browser elsewhere instead of rendering."""

    def __init__(self, location: str, status_code: int = 303) -> None:
        self.location = location
        self.status_code = status_code


class WebForbidden(Exception):
    """Raised when the signed-in user may not perform the requested UI action.

    `action` carries the audit action the refusal is recorded under, so a UI denial and the
    JSON API's denial of the same request land in the trail under the same name. It defaults to
    the role check because that is the common case.
    """

    def __init__(
        self,
        message: str = "当前用户无权执行该操作",
        *,
        action: str = "auth.rbac",
    ) -> None:
        self.message = message
        self.action = action


class WebNotFound(Exception):
    """Raised when the addressed UI resource does not exist."""

    def __init__(self, message: str = "资源不存在") -> None:
        self.message = message


@dataclass(frozen=True, slots=True)
class WebSession:
    """The authenticated user plus the raw session token behind the CSRF cookie."""

    user: User
    token: str


def new_csrf_token() -> str:
    """Return a fresh random CSRF value."""
    return secrets.token_urlsafe(32)


def cookie_secure(settings: Settings) -> bool:
    """Mark cookies `Secure` everywhere except local development and tests."""
    return settings.app_env not in {"local", "test"}


def login_url(request: Request) -> str:
    """Return the login URL, remembering where the browser was heading."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if target.startswith(LOGIN_PATH):
        return LOGIN_PATH
    return f"{LOGIN_PATH}?next={quote(target, safe='')}"


def load_web_session(request: Request, session: Session) -> WebSession:
    """Resolve the cookie session, or redirect to the login page."""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise WebRedirect(login_url(request))
    try:
        claims = decode_access_token(token, settings)
    except ValueError:
        raise WebRedirect(login_url(request)) from None
    user = get_user_by_id(session, int(claims.sub))
    if user is None or user.status != "active":
        raise WebRedirect(login_url(request))
    return WebSession(user=user, token=token)


def verify_csrf(request: Request, submitted: str | None) -> None:
    """Reject a mutating request whose CSRF field does not match its cookie."""
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise WebForbidden("CSRF 校验失败，请刷新页面后重试", action="auth.csrf")
