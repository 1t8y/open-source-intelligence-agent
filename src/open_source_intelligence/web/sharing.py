"""唯一一个不需要会话的 Web 入口。

`GET /share/{token}` 为没有账号的读者渲染一份已发布的 `PUBLIC` 报告。它刻意做成
不带前缀的独立路由器：`web/router.py` 里的其它页面全部位于 `/ui` 之下并由会话守卫，
把一条免认证路由混进那个模块，等于把例外和规则写进同一个文件。

这里有三个响应头承担着实实在在的作用：

- `Cache-Control: no-store` —— 对外分享的报告可以被撤回，被撤回的链接必须立刻
  失效。否则浏览器和代理缓存会在 `revoked_at` 落下之后继续返回正文。
- `X-Robots-Tag: noindex, nofollow` —— 一个被粘进公开频道里的 URL 不该变成
  搜索结果。这个链接是凭据，不是出版物。
- 基础模板的 `referrer` 策略（`same-origin`）—— 读者从页面点向外链时，令牌不会
  进入 `Referer` 头。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.reports.sharing import resolve_share_token
from open_source_intelligence.security.dependencies import get_db_session

from .router import templates

DbSession = Annotated[Session, Depends(get_db_session)]

router = APIRouter(tags=["share"], include_in_schema=False)


@router.get("/share/{token}", response_class=HTMLResponse)
def shared_report_page(
    request: Request,
    session: DbSession,
    token: str,
) -> HTMLResponse:
    """渲染一份分享报告；任何失败都返回同一个 404。

    `resolve_share_token` 在这些情况下返回 `None`：令牌未知、链接已撤回、链接已
    过期、报告已撤销，以及报告不再是 `PUBLIC`。这五种情况刻意渲染同一个页面：
    一旦能区分它们，任何人拿一个随机字符串就能问出某份报告是否存在。
    """
    resolved = resolve_share_token(session, token)
    if resolved is None:
        response = templates.TemplateResponse(
            request,
            "not_found.html",
            {
                "message": "这个分享链接不可用，可能已过期或被撤回",
                "current_user": None,
                "csrf_token": "",
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    else:
        report = resolved.report
        topic = session.get(Topic, report.topic_id)
        response = templates.TemplateResponse(
            request,
            "share.html",
            {
                "current_user": None,
                "csrf_token": "",
                "wide_layout": True,
                "report": report,
                "topic_name": topic.name if topic is not None else "—",
            },
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
