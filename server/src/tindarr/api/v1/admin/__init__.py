"""Administration endpoints, all under ``/api/v1/admin`` and all console-only.

Four groups, one module each: the connectors (the media server, in step 2), the
general server settings, the users, and what the AI provider has cost (step 4.5).
They share the ``admin`` tag, so every one of them needs the effective role ``admin``;
the few actions that need more say so themselves (docs/auth.md, section 7).
"""

from fastapi import APIRouter

from tindarr.api.v1.admin import connectors, settings, usage, users

router = APIRouter()
router.include_router(connectors.router)
router.include_router(settings.router)
router.include_router(users.router)
router.include_router(usage.router)
