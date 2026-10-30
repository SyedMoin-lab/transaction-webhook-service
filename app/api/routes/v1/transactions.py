from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status

from app.api.dependencies import ContainerDep
from app.db.models import IDENTIFIER_MAX_LENGTH
from app.schemas.common import ErrorResponse
from app.schemas.transactions import TransactionResponse

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get(
    "/{transaction_id}",
    response_model=list[TransactionResponse],
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    summary="Retrieve transaction status",
)
async def get_transaction(
    transaction_id: Annotated[str, Path(min_length=1, max_length=IDENTIFIER_MAX_LENGTH)],
    container: ContainerDep,
) -> list[TransactionResponse]:
    record = await container.repository.get(transaction_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return [TransactionResponse.model_validate(record)]
