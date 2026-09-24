"""Version 1 of the HTTP API (``/api/v1``). Changes inside v1 are additive only."""

from fastapi import APIRouter

from tindarr.api.v1 import admin, auth, imports, me, pairing, server, setup, swipe

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX)
router.include_router(server.router)
router.include_router(setup.router)
router.include_router(auth.router)
router.include_router(pairing.app_router)
router.include_router(pairing.router)
router.include_router(me.router)
router.include_router(imports.router)
router.include_router(swipe.router)
router.include_router(admin.router, prefix="/admin")
