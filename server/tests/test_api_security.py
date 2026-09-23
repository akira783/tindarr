"""The authenticator's authorisation step, for the cases no route reaches today."""

from datetime import UTC, datetime

import pytest

from tindarr.api.context import RequestContext
from tindarr.api.security import Authenticator, Credential
from tindarr.core.errors import ProblemError
from tindarr.storage.sessions import Session

CONTEXT = RequestContext(
    peer=None, client=None, scheme="https", host=None, host_allowed=True, trusted_peer=False
)


def setup_session() -> Session:
    now = datetime.now(UTC)
    return Session(
        id="s",
        kind="setup",
        user_id=None,
        token_hash=None,
        csrf_token="c",
        device_name=None,
        platform=None,
        app_version=None,
        created_at=now,
        last_seen_at=now,
        expires_at=now,
        reauth_at=None,
        revoked_at=None,
        revoked_reason=None,
    )


def test_a_session_without_a_user_is_never_an_administrator() -> None:
    """L2: `admin=True` must refuse a user-less session, not skip the check."""
    credential = Credential(setup_session(), None, "cookie")
    Authenticator(cookies=("setup",)).authorize(credential, CONTEXT)
    with pytest.raises(ProblemError) as caught:
        Authenticator(cookies=("setup",), admin=True).authorize(credential, CONTEXT)
    assert caught.value.code == "admin_required"
