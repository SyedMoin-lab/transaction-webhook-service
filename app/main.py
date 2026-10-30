import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import health, transactions, webhooks
from app.config import Settings, get_settings
from app.container import ServiceContainer
from app.db import create_engine
from app.logging_config import configure_logging
from app.processor import TransactionProcessor
from app.repository import TransactionRepository
from app.worker import TransactionWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    processor: TransactionProcessor | None = app.state.processor
    engine = create_engine(settings)
    repository = TransactionRepository(engine)
    worker = (
        TransactionWorker.from_settings(settings, repository, processor)
        if settings.worker_enabled
        else None
    )
    app.state.container = ServiceContainer(
        settings=settings, engine=engine, repository=repository, worker=worker
    )
    if worker is not None:
        await worker.start()
    logger.info(
        "Service started",
        extra={
            "version": settings.app_version,
            "worker_enabled": worker is not None,
            "processing_delay_seconds": settings.processing_delay_seconds,
        },
    )
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        await engine.dispose()
        logger.info("Service stopped")


def create_app(
    settings: Settings | None = None, processor: TransactionProcessor | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Transaction Webhook Service",
        description=(
            "Receives payment processor transaction webhooks, acknowledges them immediately "
            "and processes them reliably in the background."
        ),
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.processor = processor

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(transactions.router)
    return app
