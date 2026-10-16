"""Normalized collector data structures and errors."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

MAX_PER_PAGE: Final[int] = 100
"""Largest page both providers honour, and therefore the largest a walk may ask for."""


class CollectorError(RuntimeError):
    """Base error for external collection failures."""


class SourceUnavailableError(CollectorError):
    """Raised when a source cannot return valid data after retries."""


@dataclass(frozen=True, slots=True)
class CollectedRepository:
    """Provider-independent repository snapshot used by the ingestion service."""

    provider: str
    provider_repo_id: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    license: str | None
    topics: tuple[str, ...]
    stars: int
    forks: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CollectedPage:
    """One page of a provider's paged search result.

    `has_more` is the provider's own verdict rather than a guess from the item count,
    because the two coincide only by accident. GitHub states it in the `Link` header and
    stops offering `rel="next"` at its 1000-result search cap; an offset provider states
    it by returning a short page. The walker stops on `has_more` being false, so getting
    the boundary wrong means either a truncated history or one wasted request.
    """

    page: int
    items: tuple[CollectedRepository, ...]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready summary of the page, without the rows themselves."""
        return {"page": self.page, "item_count": len(self.items), "has_more": self.has_more}


class RepositoryCollector(Protocol):
    """Contract implemented by each repository provider adapter."""

    provider: str
    display_name: str
    base_url: str

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """Collect up to the requested number of repositories."""
        ...


@runtime_checkable
class PaginatedRepositoryCollector(RepositoryCollector, Protocol):
    """A repository source that can be walked one page at a time.

    `collect` answers "give me N repositories"; this answers "give me page P". A resumable
    walk needs the second question: the cursor it persists is just a page number, so
    resuming is calling this again with a larger one rather than replaying earlier pages.

    `describe_query` exists because a job has to record what it asked for. Deriving that
    from the cursor would be a guess; asking the adapter is the only place that knows which
    search it is walking, and it keeps one definition of the query rather than a copy in
    the job row that can drift from the request actually sent.
    """

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """Return one 1-based page of at most `per_page` repositories."""
        ...

    def describe_query(self) -> dict[str, Any]:
        """Return the search parameters this collector pages, for a job's provenance."""
        ...


def clamp_per_page(per_page: int) -> int:
    """Reject a page size outside what both providers accept."""
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise ValueError(f"per_page must be between 1 and {MAX_PER_PAGE}")
    return per_page


READABLE_CONTENT_TYPES: Final[tuple[str, ...]] = ("readme",)
"""Content kinds a source may hand back, mirroring the `repository_contents` CHECK."""


@dataclass(frozen=True, slots=True)
class FetchedContent:
    """One provider's long-form repository text, still uncleaned.

    `content` is deliberately raw: cleaning and truncation belong to the single write path
    into `repository_contents`, so every caller shares one definition of what "cached"
    means. Cleaning here would let the cached text and the digest of it drift apart from
    what a second cleaner would produce.

    `source_url` has to be an `https` link because report citations carry it out to
    readers, and `reports/citations.py` rejects anything else.
    """

    provider: str
    full_name: str
    content_type: str
    source_url: str
    content: str
    fetched_at: datetime


@runtime_checkable
class RepositoryContentSource(Protocol):
    """A repository source that can also hand back cached long-form text.

    Kept separate from `RepositoryCollector` rather than folded into it, because the two
    capabilities are asked for by different stages and are not equally available: a search
    API answers "which repositories exist" for every provider, while "what does this
    repository's README say" is answered differently (or not at all) by each. A provider
    that cannot answer simply does not implement this, and the content stage records its
    repositories as unsupported instead of demanding an empty method.

    `fetch_readme` returns `None` when the source *answered* that there is no such content
    (GitHub's `404`). Raising `SourceUnavailableError` is reserved for "the source could not
    answer" - a refusal, a timeout, or a body that is not the documented shape. The two are
    different facts about the world and the ledger keeps them apart: `missing` says the
    repository has no README, `failed` says we did not find out.
    """

    provider: str

    def fetch_readme(self, full_name: str) -> FetchedContent | None:
        """Return the repository README, or `None` when the source says there is none."""
        ...


def require_page(page: int) -> int:
    """Reject a page number that is not 1-based."""
    if page < 1:
        raise ValueError("page must be at least 1")
    return page


def nonnegative_int(value: object, field_name: str) -> int:
    """Convert a provider value to a nonnegative integer or fail clearly."""
    if isinstance(value, bool):
        converted = int(value)
    elif isinstance(value, int):
        converted = value
    elif isinstance(value, float) and value.is_integer():
        converted = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        converted = int(value)
    else:
        raise SourceUnavailableError(f"invalid integer field: {field_name}")
    if converted < 0:
        raise SourceUnavailableError(f"negative integer field: {field_name}")
    return converted
