"""The background work around the media server: the sweep and the Quick Connect probe."""

from asyncio import all_tasks
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from tests.support import (
    ADMIN_NAME,
    API,
    FakeClock,
    FakeInternet,
    build_app,
    claim,
    console_client,
    console_headers,
    run,
    seed_jellyfin,
    set_up_server,
)
from tests.support.flows import configure_media_server
from tindeerr.auth.sync import UserSync
from tindeerr.jobs.media_server import (
    handle_sweep_job,
    quick_connect_probe_job,
    user_sync_job,
)


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def start_quick_connect(client: Any) -> str:
    handle: str = client.post(
        f"{API}/auth/quick-connect", json={"purpose": "sign_in"}, headers=console_headers()
    ).json()["handle"]
    return handle


def test_an_abandoned_approval_is_collected_and_logged_out(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        start_quick_connect(client)
        # Approved in a Jellyfin client, then nobody ever comes back for it.
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", ADMIN_NAME)
        internet.media.logouts.clear()
        clock.advance(5 * 60 + 1)
        services: Any = app.state.services
        run(client, handle_sweep_job(services.quick_connect).run_once)
    assert internet.media.logouts == [f"user-token-{ADMIN_NAME}"]


def test_an_approval_survives_another_client_touching_the_registry(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        start_quick_connect(client)
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", ADMIN_NAME)
        internet.media.logouts.clear()
        clock.advance(5 * 60 + 1)
        # Somebody else starts a sign-in, which prunes the expired handle first.
        start_quick_connect(client)
        services: Any = app.state.services
        run(client, handle_sweep_job(services.quick_connect).run_once)
    assert internet.media.logouts == [f"user-token-{ADMIN_NAME}"]


def test_a_handle_nobody_approved_leaves_nothing_behind(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        start_quick_connect(client)
        internet.media.logouts.clear()
        clock.advance(5 * 60 + 1)
        services: Any = app.state.services
        run(client, handle_sweep_job(services.quick_connect).run_once)
        assert services.handles.outstanding == 0
    assert internet.media.logouts == []


def test_the_sweep_survives_an_unreachable_media_server(
    app: FastAPI, internet: FakeInternet, clock: FakeClock, log_stream: Any
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        start_quick_connect(client)
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", ADMIN_NAME)
        clock.advance(5 * 60 + 1)
        internet.media.offline = True
        services: Any = app.state.services
        run(client, handle_sweep_job(services.quick_connect).run_once)
    assert "could not clean up abandoned Quick Connect approvals" in log_stream.getvalue()


def test_the_sweep_does_nothing_when_there_is_nothing_to_sweep(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        assert run(client, services.quick_connect.sweep) == 0


def test_the_probe_makes_quick_connect_visible_in_server_info(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        services.sign_in.quick_connect.remember(enabled=None)
        assert "quick_connect" not in client.get(f"{API}/server/info").json()["auth_methods"]
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
        methods = client.get(f"{API}/server/info").json()["auth_methods"]
    assert methods == ["password", "quick_connect"]
    assert internet.media.quick_connect_enabled is True


def test_the_probe_stops_advertising_it_when_it_is_switched_off(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        internet.media.quick_connect_enabled = False
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
        methods = client.get(f"{API}/server/info").json()["auth_methods"]
    assert methods == ["password"]


def test_the_probe_forgets_the_answer_when_the_server_is_unreachable(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
        internet.media.offline = True
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
        methods = client.get(f"{API}/server/info").json()["auth_methods"]
    assert methods == ["password"]


def test_the_probe_says_no_for_a_server_without_quick_connect(
    data_dir: Path, clock: FakeClock
) -> None:
    internet = seed_jellyfin(FakeInternet())
    internet.media.kind = "emby"
    app = build_app(data_dir, clock=clock, internet=internet)
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf, server_type="emby").status_code == 200
        services: Any = app.state.services
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
    assert services.sign_in.quick_connect.cached is False


def test_the_probe_is_quiet_before_the_media_server_is_configured(app: FastAPI) -> None:
    with console_client(app) as client:
        services: Any = app.state.services
        run(client, quick_connect_probe_job(services.quick_connect).run_once)
        assert services.sign_in.quick_connect.cached is None


def test_the_user_sync_job_logs_what_it_changed(
    app: FastAPI, internet: FakeInternet, log_stream: Any
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        del internet.media.users[ADMIN_NAME]
        sync = UserSync(services.engine, services.connector, services.clock, services.install_id)
        run(client, user_sync_job(sync).run_once)
    assert "media server user sync" in log_stream.getvalue()


def test_the_jobs_are_started_with_the_application(app: FastAPI) -> None:
    async def running() -> list[str]:
        return [task.get_name() for task in all_tasks()]

    with console_client(app) as client:
        names = run(client, running)
    # Purge, user sync, handle sweep and the Quick Connect probe.
    assert sorted(name for name in names if name.startswith("tindeerr.jobs.")) == [
        "tindeerr.jobs.handle_sweep",
        "tindeerr.jobs.purge",
        "tindeerr.jobs.quick_connect_probe",
        "tindeerr.jobs.user_sync",
    ]


def test_one_dead_handle_does_not_stop_the_sweep(
    app: FastAPI, internet: FakeInternet, clock: FakeClock, log_stream: Any
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        start_quick_connect(client)
        secret = f"qc-secret-{len(internet.media.quick_connect)}"
        internet.media.approve(secret, ADMIN_NAME)
        # Jellyfin forgot the request in the meantime.
        del internet.media.quick_connect[secret]
        clock.advance(5 * 60 + 1)
        services: Any = app.state.services
        assert run(client, services.quick_connect.sweep) == 0
    assert "could not clean up an abandoned Quick Connect approval" in log_stream.getvalue()
