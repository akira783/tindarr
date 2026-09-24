"""What the AI provider has cost, per user and per day.

The one screen that answers "why is my bill what it is", and the one place a household
can see that somebody's deck is failing every night: a row carries the failures beside
the generations, so a provider that has been rejecting a key for a week is visible
without reading a log.

It is a **listing of rows, not a report**. No totals are computed here and nothing is
hidden: the console decides what to add up, and an administrator who exports it gets the
same numbers. Days with no usage simply have no row.
"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from tindarr.api.deps import Services
from tindarr.api.security import AdminSession
from tindarr.api.v1.models import UsageDayResponse, UsageResponse
from tindarr.storage import usage as usage_repository

router = APIRouter(tags=["admin", "console"])

Days = Annotated[int, Query(ge=1, le=90, description="How many days back to report.")]


@router.get("/usage", operation_id="getUsage", summary="AI usage per user and per day")
async def get_usage(services: Services, session: AdminSession, days: Days = 30) -> UsageResponse:
    """Return every user's usage over the last ``days`` days, most recent day first."""
    since = services.clock.now() - timedelta(days=days - 1)
    async with services.engine.connect() as connection:
        rows = await usage_repository.list_usage(connection, since=since)
    return UsageResponse(days=[UsageDayResponse.of(row) for row in rows])
