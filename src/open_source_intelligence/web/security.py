"""为服务端渲染的管理界面提供 Cookie 会话与 CSRF 防护。

界面沿用 API 签发的同一份 JWT 完成认证，但把它放进 `HttpOnly` Cookie，脚本读不到它
（`docs/security-and-privacy.md` §3.1）。每个会改变状态的表单还要额外携带一个双提交
CSRF 令牌：随机值存在 Cookie 里，同时回填到隐藏字段，两者必须逐字节一致。
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
    """由界面依赖抛出，用于把浏览器送去别处，而不是渲染页面。"""

    def __init__(self, location: str, status_code: int = 303) -> None:
        self.location = location
        self.status_code = status_code


class WebForbidden(Exception):
    """当已登录用户无权执行所请求的界面操作时抛出。

    `action` 携带这次拒绝记入审计时使用的动作名，于是同一个请求在界面上被拒、
    在 JSON API 上被拒，都会以同一个名字落进审计链。它默认取角色校验对应的动作，
    因为那是最常见的情形。
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
    """当被寻址的界面资源不存在时抛出。"""

    def __init__(self, message: str = "资源不存在") -> None:
        self.message = message


@dataclass(frozen=True, slots=True)
class WebSession:
    """已认证的用户，以及 CSRF Cookie 背后的原始会话令牌。"""

    user: User
    token: str


def new_csrf_token() -> str:
    """返回一个新的随机 CSRF 值。"""
    return secrets.token_urlsafe(32)


def cookie_secure(settings: Settings) -> bool:
    """除本地开发与测试之外，一律把 Cookie 标记为 `Secure`。"""
    return settings.app_env not in {"local", "test"}


def login_url(request: Request) -> str:
    """返回登录地址，同时记住浏览器原本要去哪里。"""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if target.startswith(LOGIN_PATH):
        return LOGIN_PATH
    return f"{LOGIN_PATH}?next={quote(target, safe='')}"


def load_web_session(request: Request, session: Session) -> WebSession:
    """解析 Cookie 会话，失败则重定向到登录页。"""
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
    """拒绝 CSRF 字段与 Cookie 不一致的写请求。"""
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise WebForbidden("CSRF 校验失败，请刷新页面后重试", action="auth.csrf")
