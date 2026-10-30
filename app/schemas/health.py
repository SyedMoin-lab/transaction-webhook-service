from typing import Literal

from pydantic import BaseModel

from app.schemas.common import UtcTimestamp


class HealthResponse(BaseModel):
    status: Literal["HEALTHY", "UNHEALTHY"]
    current_time: UtcTimestamp
