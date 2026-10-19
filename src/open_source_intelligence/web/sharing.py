"""The one web surface that does not require a session.

`GET /share/{token}` renders a published `PUBLIC` report for a reader with no account. It is
deliberately a separate router with no prefix: every other page in `web/router.py` sits under
`/ui` and is guarded by a session, and mixing an unauthenticated route into that module would
put the exception in the same file as the rule.

Three response headers carry real weight here:

- `Cache-Control: no-store` - a shared report can be revoked, and a revoked link must stop
  working. Browser and proxy caches would keep serving the body after `revoked_at` is set.
- `X-Robots-Tag: noindex, nofollow` - a URL pasted into a public channel should not become a
  search result. The link is a credential, not a publication.
- the base template's `referrer` policy (`same-origin`) - keeps the token out of the `Referer`
  header when a reader follows an outbound link from the page.
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
    """Render a shared report, or the same 404 for every failure.

    `resolve_share_token` returns `None` for an unknown token, a revoked link, an expired link,
    a retracted report, and a report that is no longer `PUBLIC`. All five render one identical
    page on purpose: telling them apart would let anybody holding a random string ask whether a
    report exists.
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
