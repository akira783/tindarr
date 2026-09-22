import pytest

from tindeerr.core.errors import ProblemError
from tindeerr.storage.settings import SettingLockedError


def test_problem_error_carries_its_fields() -> None:
    error = ProblemError(429, "rate_limited", "Slow down.", {"Retry-After": "2"})
    assert (error.status, error.code, error.detail) == (429, "rate_limited", "Slow down.")
    assert dict(error.headers) == {"Retry-After": "2"}
    assert str(error) == "Slow down."
    assert str(ProblemError(404, "not_found")) == "not_found"


@pytest.mark.parametrize("status", [200, 302, 399, 600])
def test_problem_error_needs_an_error_status(status: int) -> None:
    with pytest.raises(ValueError, match="error status"):
        ProblemError(status, "x")


def test_problem_headers_are_read_only() -> None:
    error = ProblemError(429, "rate_limited", headers={"Retry-After": "2"})
    with pytest.raises(TypeError):
        error.headers["Retry-After"] = "0"  # pyright: ignore[reportIndexIssue]


def test_setting_locked_error_is_a_problem() -> None:
    error = SettingLockedError("media_server_url")
    assert isinstance(error, ProblemError)
    assert (error.status, error.code, error.name) == (409, "setting_locked", "media_server_url")
    assert "TINDEERR_MEDIA_SERVER_URL" in (error.detail or "")
