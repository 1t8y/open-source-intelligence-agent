"""服务存活探针端点与响应契约。"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """存活探针返回的稳定响应体。"""

    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """返回进程存活状态，不依赖任何外部服务。"""
    from open_source_intelligence import __version__

    return HealthResponse(
        status="ok",
        service="open-source-intelligence-agent",
        version=__version__,
    )
