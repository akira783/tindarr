"""Version 1 of the HTTP API (``/api/v1``). Changes inside v1 are additive only."""

from fastapi import APIRouter

from tindeerr.api.v1 import server

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX)
router.include_router(server.router)
