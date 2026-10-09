"""Service liveness endpoint and response contract."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Stable response returned by the service liveness endpoint."""

    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return process liveness without depending on external services."""
    from open_source_intelligence import __version__

    return HealthResponse(
        status="ok",
        service="open-source-intelligence-agent",
        version=__version__,
    )
