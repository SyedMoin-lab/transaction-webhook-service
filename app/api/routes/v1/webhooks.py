import asyncio
import logging

from fastapi import APIRouter, Depends, status

from app.api.dependencies import ContainerDep, verify_webhook_signature
from app.schemas.common import ErrorResponse
from app.schemas.transactions import TransactionWebhook, WebhookAcknowledgement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/transactions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAcknowledgement,
    dependencies=[Depends(verify_webhook_signature)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
    summary="Receive a transaction webhook",
)
async def receive_transaction_webhook(
    payload: TransactionWebhook, container: ContainerDep
) -> WebhookAcknowledgement:
    async with asyncio.timeout(container.settings.webhook_persist_timeout_seconds):
        created = await container.repository.create_if_absent(payload)

    if created and container.worker is not None:
        container.worker.notify()

    logger.info(
        "Transaction webhook accepted" if created else "Duplicate transaction webhook ignored",
        extra={"transaction_id": payload.transaction_id, "duplicate": not created},
    )
    return WebhookAcknowledgement(transaction_id=payload.transaction_id, duplicate=not created)
