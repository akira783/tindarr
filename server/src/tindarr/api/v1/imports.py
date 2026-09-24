"""File imports and the calibration grid: the two ways of saying "I have seen that".

Both write ``watch_history`` and neither writes a vote, which is the distinction the
whole of roadmap 4.4 turns on and the one a reader of this file should carry away: what
lands here is excluded from the deck and read as engagement, and it moves no statistic,
no vote count and no calibration progress the deck reports.

**The upload is the request body, not a form.** A multipart parse would mean another
dependency and a temporary file holding somebody's viewing history on disk; the bytes
here are read from the stream, bounded as they arrive, handed to the parser and
forgotten. The file name never reaches the server at all, which is the simplest way of
not storing it.

**Reading a file and identifying it are separate.** Parsing is cheap and its failure is
something the user must be told at once, so it happens in this request; identifying
seventy titles is a hundred TMDb calls, so it happens in a task and the ``imports`` row
is the status. A browser therefore never holds a connection open for a minute, and an
import survives the tab being closed.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, status

from tindarr.api.deps import Services
from tindarr.api.security import Context, SharedSession, WebSession
from tindarr.api.v1.models import (
    GridResponse,
    GridSubmitInput,
    GridSubmitResponse,
    GridTitleResponse,
    ImportListResponse,
    ImportResponse,
    ImportReviewDecisionInput,
    ImportReviewEntryResponse,
    ImportReviewListResponse,
)
from tindarr.auth import errors
from tindarr.storage import imports as import_repository
from tindarr.swipe.calibration import GRID_SIZE, MAX_GRID_PAGE, MAX_GRID_SIZE
from tindarr.swipe.imports import MAX_UPLOAD_BYTES
from tindarr.swipe.watched import import_too_large

router = APIRouter(prefix="/swipe", tags=["swipe"])

logger = logging.getLogger(__name__)

ImportId = Annotated[str, Path(description="The import.", max_length=64)]
EntryId = Annotated[str, Path(description="The queued row.", max_length=64)]
ReviewLimit = Annotated[int, Query(ge=1, le=100, description="How many entries to return.")]
GridPage = Annotated[int, Query(ge=1, le=MAX_GRID_PAGE, description="Which wall.")]
GridSize = Annotated[int, Query(ge=1, le=MAX_GRID_SIZE, description="How many posters.")]


async def read_upload(request: Request) -> bytes:
    """Read the uploaded file, refusing anything past the cap as it arrives.

    The declared length is checked first because it is free, and the bytes are counted
    as well because a ``Content-Length`` is a claim and the body is the fact. Nothing is
    written to disk at any point.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise import_too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise import_too_large()
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/imports",
    operation_id="startImport",
    summary="Upload a Netflix, IMDb or Letterboxd export",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["console"],
)
async def start_import(
    request: Request, services: Services, context: Context, session: WebSession
) -> ImportResponse:
    """Read an upload, open an import for it, and identify its titles in the background."""
    services.limits.imports.hit(context.rate_limit_key)
    payload = await read_upload(request)
    record, parsed = await services.imports.start(session.signed_in_user.id, payload)
    services.import_runner.submit(record, parsed)
    return ImportResponse.of(record)


@router.get("/imports", operation_id="listImports", summary="The caller's file imports")
async def list_imports(services: Services, session: SharedSession) -> ImportListResponse:
    """Return this user's imports, most recent first."""
    async with services.engine.connect() as connection:
        rows = await import_repository.list_for_user(connection, session.signed_in_user.id)
    return ImportListResponse(imports=[ImportResponse.of(row) for row in rows])


@router.get(
    "/imports/{import_id}", operation_id="getImport", summary="One import, and what it produced"
)
async def get_import(
    import_id: ImportId, services: Services, session: SharedSession
) -> ImportResponse:
    """Return one of the caller's imports; another user's id is a ``404``."""
    async with services.engine.connect() as connection:
        record = await import_repository.get(connection, session.signed_in_user.id, import_id)
    if record is None:
        raise errors.not_found("No such import.")
    return ImportResponse.of(record)


@router.delete(
    "/imports/{import_id}",
    operation_id="deleteImport",
    summary="Forget an import and everything that source told us",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["console"],
)
async def delete_import(import_id: ImportId, services: Services, session: WebSession) -> None:
    """Delete the import and the history rows its source wrote for this user."""
    await services.imports.forget(session.signed_in_user.id, import_id)


@router.get(
    "/imports/{import_id}/review",
    operation_id="listImportReview",
    summary="The rows this import refused to guess at",
)
async def list_import_review(
    import_id: ImportId, services: Services, session: SharedSession, limit: ReviewLimit = 50
) -> ImportReviewListResponse:
    """Return the open questions of one import, oldest first."""
    user_id = session.signed_in_user.id
    async with services.engine.connect() as connection:
        if await import_repository.get(connection, user_id, import_id) is None:
            raise errors.not_found("No such import.")
        entries = await import_repository.list_reviews(connection, user_id, import_id, limit=limit)
        pending = await import_repository.count_pending(connection, user_id, import_id)
    return ImportReviewListResponse(
        entries=[ImportReviewEntryResponse.of(entry) for entry in entries], pending=pending
    )


@router.post(
    "/imports/{import_id}/review/{entry_id}",
    operation_id="decideImportReview",
    summary="Answer one row the import could not settle",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["console"],
)
async def decide_import_review(
    import_id: ImportId,
    entry_id: EntryId,
    body: ImportReviewDecisionInput,
    services: Services,
    session: WebSession,
) -> None:
    """Accept one of the entry's own candidates, or reject the row.

    ``import_id`` is in the path for the client's sake; the entry is looked up by its
    own id **scoped to the caller**, so neither id can be used to reach another account.
    """
    user_id = session.signed_in_user.id
    if body.decision == "reject" or body.title is None:
        await services.imports.reject(user_id, entry_id)
        return
    await services.imports.accept(user_id, entry_id, body.title.ref)


@router.get(
    "/calibration/grid",
    operation_id="getCalibrationGrid",
    summary="A wall of famous posters to tick",
)
async def get_calibration_grid(
    services: Services, session: SharedSession, page: GridPage = 1, size: GridSize = GRID_SIZE
) -> GridResponse:
    """Return the next wall, never repeating a title the caller has answered about."""
    found = await services.grid.build(session.signed_in_user.id, page=page, size=size)
    return GridResponse(page=page, titles=[GridTitleResponse.of(row) for row in found])


@router.post(
    "/calibration/grid",
    operation_id="submitCalibrationGrid",
    summary="Record what the caller ticked on a wall",
)
async def submit_calibration_grid(
    body: GridSubmitInput, services: Services, session: SharedSession
) -> GridSubmitResponse:
    """Write the answers as history. Neither answer is a vote."""
    ticks = [answer.tick for answer in body.answers]
    recorded = await services.grid.submit(session.signed_in_user.id, ticks)
    return GridSubmitResponse(recorded=recorded)
