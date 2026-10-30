from fastapi import APIRouter

from app.api.routes import health
from app.api.routes.v1 import transactions, webhooks

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(webhooks.router)
v1_router.include_router(transactions.router)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(v1_router)
