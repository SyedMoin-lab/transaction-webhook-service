import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.container import ServiceContainer


def get_container(request: Request) -> ServiceContainer:
    container: ServiceContainer = request.app.state.container
    return container


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]


async def verify_webhook_signature(request: Request, container: ContainerDep) -> None:
    settings = container.settings
    if settings.webhook_secret is None:
        return

    signature = request.headers.get(settings.webhook_signature_header)
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature"
        )

    body = await request.body()
    expected = hmac.new(
        settings.webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    provided = signature.strip().removeprefix("sha256=").lower()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
        )
