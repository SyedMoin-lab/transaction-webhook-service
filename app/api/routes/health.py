import asyncio
import logging

from fastapi import APIRouter, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.dependencies import ContainerDep
from app.db import ping
from app.schemas import HealthResponse, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def is_database_healthy(engine: AsyncEngine, timeout_seconds: float) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds):
            await ping(engine)
    except Exception:
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True


@router.get(
    "/",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    summary="Service health check",
)
@router.head("/", response_model=HealthResponse, include_in_schema=False)
async def health_check(container: ContainerDep, response: Response) -> HealthResponse:
    healthy = await is_database_healthy(
        container.engine, container.settings.health_check_timeout_seconds
    )
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="HEALTHY" if healthy else "UNHEALTHY",
        current_time=utc_now(),
    )
