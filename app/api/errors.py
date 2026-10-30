import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = "5"
UNAVAILABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    PoolTimeoutError,
    OSError,
)


async def service_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Dependency unavailable while handling request",
        exc_info=exc,
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Service temporarily unavailable, please retry"},
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled error while handling request",
        exc_info=exc,
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    for exception_type in UNAVAILABLE_EXCEPTIONS:
        app.add_exception_handler(exception_type, service_unavailable_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
