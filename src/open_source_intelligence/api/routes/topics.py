"""Topic endpoints: the monitoring-scope configuration.

`docs/api-contract.md` §3.2 is the contract; this module shapes requests and responses and
delegates every decision. Two delegation choices are worth naming:

* **The single-row read goes through `authorize_topic_read`**, the same function the repository
  catalog uses to validate a `topic_id` filter. There is one implementation of "may this caller see
  this topic", and it is not in this file.
* **The writes call the service and let its exceptions carry the answer.** A missing topic becomes
  `404` through the `TopicError` handler rather than through a pre-fetch in the route, so there is
  one existence check per operation instead of two that can disagree.

Nothing here triggers a research run. `POST /topics/{id}/run` is absent on purpose: a run spends
model budget, and that trigger belongs to the scheduler and the operator CLI (§3.2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.security.authorization import (
    TOPIC_READ_ROLES,
    TOPIC_WRITE_ROLES,
    authorize_topic_read,
)
from open_source_intelligence.security.dependencies import (
    get_db_session,
    request_id,
    require_roles,
)
from open_source_intelligence.topics import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    create_topic,
    delete_topic,
    list_topics,
    require_usable_name,
    update_topic,
)

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])

ReadUser = Annotated[User, Depends(require_roles(*TOPIC_READ_ROLES))]
WriteUser = Annotated[User, Depends(require_roles(*TOPIC_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

PublishMode = Literal["AUTO", "AUTO_WITH_DELAY", "MANUAL"]

TopicTerm = Annotated[str, Field(min_length=1, max_length=64)]


def _require_usable_name(value: str) -> str:
    """Validate the `name` field with the writer's own definition of a usable name.

    The body is a call to `topics.require_usable_name` and nothing else, which is the point: the
    boundary does not restate the rule, it invokes it. `require_usable_name` raises
    `TopicNameBlank`, a `ValueError`, so Pydantic turns the refusal into a `422` naming the field
    while the same exception is what a script calling the service directly receives.
    """
    return require_usable_name(value)


#: A topic name validated and trimmed at the boundary.
TopicName = Annotated[
    str,
    AfterValidator(_require_usable_name),
    Field(min_length=1, max_length=128),
]


class TopicResponse(BaseModel):
    """One monitoring topic."""

    id: int
    name: str
    description: str | None
    languages: list[str] | None
    keywords: list[str] | None
    min_stars: int
    window_days: int
    max_candidates: int
    publish_mode: str
    enabled: bool
    created_by: int
    created_at: datetime
    updated_at: datetime


class TopicListResponse(BaseModel):
    """One page of visible topics."""

    items: list[TopicResponse]
    page: int
    page_size: int
    total: int


class _TopicFields(BaseModel):
    """The optional half of a topic request: everything the two required columns are not.

    A shared base rather than a create model that the update model inherits from. Inheritance would
    make `PATCH` redeclare `name` and `publish_mode` as optional, and widening an inherited field is
    the one override direction the type checker rejects - correctly, because a caller holding the
    subclass would then be able to clear a field the base promises is set. Splitting the fields the
    two requests genuinely share keeps `POST` required and `PATCH` optional without either model
    contradicting the other.
    """

    description: str | None = Field(default=None, max_length=2000)
    languages: list[TopicTerm] | None = Field(default=None, max_length=32)
    keywords: list[TopicTerm] | None = Field(default=None, max_length=32)
    min_stars: int | None = Field(default=None, ge=0)
    window_days: int | None = Field(default=None, ge=1, le=365)
    max_candidates: int | None = Field(default=None, ge=1, le=200)
    enabled: bool | None = None


class TopicCreateRequest(_TopicFields):
    """The fields a new topic must carry.

    Every optional field defaults to `None` and the request model restates none of the column
    defaults: a field the caller did not send is absent from `model_dump(exclude_unset=True)`, so
    the columns stay the single definition of what a new topic looks like.
    """

    name: TopicName
    publish_mode: PublishMode


class TopicUpdateRequest(_TopicFields):
    """The fields a PATCH may carry: the same set, all optional.

    ``name`` and ``publish_mode`` become optional here, which is the only difference between
    creating a topic and changing one.
    """

    name: TopicName | None = None
    publish_mode: PublishMode | None = None


class TopicDeleteResponse(BaseModel):
    """The result of deleting an unreferenced topic."""

    topic_id: int
    deleted: bool
    unlinked_candidate_count: int


def _to_response(topic: Topic) -> TopicResponse:
    """Shape one topic row for egress.

    `languages_json` / `keywords_json` leave as `languages` / `keywords`. The `_json` suffix names
    the storage form, not the field - the catalog makes the same choice with `topics_json`. A null
    list means "no restriction of this kind", which is what the column holds when it is not set.
    """
    return TopicResponse(
        id=topic.id,
        name=topic.name,
        description=topic.description,
        languages=list(topic.languages_json) if topic.languages_json else None,
        keywords=list(topic.keywords_json) if topic.keywords_json else None,
        min_stars=topic.min_stars,
        window_days=topic.window_days,
        max_candidates=topic.max_candidates,
        publish_mode=topic.publish_mode,
        enabled=topic.enabled,
        created_by=topic.created_by,
        created_at=topic.created_at,
        updated_at=topic.updated_at,
    )


@router.get("", response_model=TopicListResponse)
def get_topics(
    user: ReadUser,
    session: DbSession,
    enabled: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> TopicListResponse:
    """Return one page of the topics this caller may see.

    `enabled=false` as a non-`ADMIN` yields an empty page rather than a `403`: the visibility rule
    and the requested filter are intersected, and "there are no disabled topics you may see" is a
    true answer to the question that was asked.
    """
    result = list_topics(
        session,
        user=user,
        enabled=enabled,
        query=q,
        page=page,
        page_size=page_size,
    )
    return TopicListResponse(
        items=[_to_response(topic) for topic in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get("/{topic_id}", response_model=TopicResponse)
def get_topic(
    request: Request,
    topic_id: int,
    user: ReadUser,
    session: DbSession,
) -> TopicResponse:
    """Return one topic, or refuse in the two ways the catalog's `topic_id` filter refuses."""
    topic = authorize_topic_read(session, request, user, topic_id=topic_id, action="topic.read")
    return _to_response(topic)


@router.post("", response_model=TopicResponse, status_code=status.HTTP_201_CREATED)
def create_topic_endpoint(
    payload: TopicCreateRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicResponse:
    """Create one monitoring topic."""
    provided = payload.model_dump(exclude_unset=True)
    name = provided.pop("name")
    publish_mode = provided.pop("publish_mode")
    topic = create_topic(
        session,
        actor_id=user.id,
        name=name,
        publish_mode=publish_mode,
        values=provided,
        request_id=request_id(request),
    )
    return _to_response(topic)


@router.patch("/{topic_id}", response_model=TopicResponse)
def update_topic_endpoint(
    topic_id: int,
    payload: TopicUpdateRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicResponse:
    """Apply the fields the caller sent to one topic.

    `exclude_unset=True` is load-bearing: it is what makes "did not send `description`" and "sent
    `description: null`" two different requests rather than one.
    """
    topic, _changed = update_topic(
        session,
        actor_id=user.id,
        topic_id=topic_id,
        values=payload.model_dump(exclude_unset=True),
        request_id=request_id(request),
    )
    return _to_response(topic)


@router.delete("/{topic_id}", response_model=TopicDeleteResponse)
def delete_topic_endpoint(
    topic_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicDeleteResponse:
    """Delete an unreferenced topic, or refuse with the tables that still point at it."""
    unlinked = delete_topic(
        session,
        actor_id=user.id,
        topic_id=topic_id,
        request_id=request_id(request),
    )
    return TopicDeleteResponse(
        topic_id=topic_id,
        deleted=True,
        unlinked_candidate_count=unlinked,
    )
