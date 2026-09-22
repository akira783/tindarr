"""Liveness probe."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["server"])


class Health(BaseModel):
    """``GET /healthz`` response."""

    status: Literal["ok"] = "ok"


@router.get("/healthz", operation_id="health", summary="Liveness probe")
async def health() -> Health:
    """Report that the process is up, without touching the database or the network."""
    return Health()
