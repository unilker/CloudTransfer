"""The HTTP surface, driven in-process against the in-memory providers."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE, get_settings
from cloudtransfer.crypto import encrypt
from cloudtransfer.db import dispose_db, session_scope
from cloudtransfer.models import Account, Job
from cloudtransfer.oauth import GOOGLE_WRITE_SCOPE


@pytest.fixture
async def api(tmp_path, drive, onedrive, monkeypatch):
    """A running app whose providers are the fakes and whose DB is temporary."""
    monkeypatch.setenv("CT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CT_SECRET_KEY", "api-test-secret")
    monkeypatch.setenv("CT_CHUNK_SIZE", str(ONEDRIVE_CHUNK_MULTIPLE * 2))
    monkeypatch.setenv("CT_SIMPLE_UPLOAD_MAX", str(ONEDRIVE_CHUNK_MULTIPLE // 2))
    monkeypatch.setenv("CT_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("CT_GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("CT_MS_CLIENT_ID", "mid")
    monkeypatch.setenv("CT_MS_CLIENT_SECRET", "msecret")
    get_settings.cache_clear()
    await dispose_db()

    async def token() -> str:
        return "token"

    from cloudtransfer.engine import worker as worker_module
    from cloudtransfer.web import deps
    from cloudtransfer.web.app import create_app

    # All four adapters, so either direction stays in memory.
    monkeypatch.setattr(worker_module, "GoogleDriveSource", lambda *a, **k: drive)
    monkeypatch.setattr(worker_module, "GoogleDriveDest", lambda *a, **k: drive)
    monkeypatch.setattr(worker_module, "OneDriveSource", lambda *a, **k: onedrive)
    monkeypatch.setattr(worker_module, "OneDriveDest", lambda *a, **k: onedrive)
    monkeypatch.setattr(
        worker_module, "TokenManager", lambda *a, **k: SimpleNamespace(get_token=token)
    )
    worker_module._runner = None

    from cloudtransfer.web import routes_jobs

    monkeypatch.setattr(
        routes_jobs,
        "build_providers",
        lambda direction, settings, client: worker_module.build_providers(
            direction, settings, client
        ),
    )

    app = create_app()
    app.dependency_overrides[deps.drive_source] = lambda: drive
    app.dependency_overrides[deps.drive_dest] = lambda: drive
    app.dependency_overrides[deps.onedrive_dest] = lambda: onedrive

    async with app.router.lifespan_context(app):
        # Both accounts connected: the state every transfer endpoint expects.
        # Writing the rows directly also exercises the real token encryption.
        settings = get_settings()
        secret = settings.resolve_secret_key()
        async with session_scope(settings) as session:
            for provider, email, scope in (
                ("google", "tester@example.com", f"{GOOGLE_WRITE_SCOPE} openid email"),
                ("microsoft", "tester@contoso.example", "offline_access Files.ReadWrite"),
            ):
                session.add(
                    Account(
                        provider=provider,
                        display_name="Tester",
                        email=email,
                        access_token_enc=encrypt(secret, "access-token"),
                        refresh_token_enc=encrypt(secret, "refresh-token"),
                        expires_at=time.time() + 3600,
                        # A real connection records what was granted; the
                        # reverse direction is refused without write access.
                        scope=scope,
                    )
                )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    await dispose_db()
    get_settings.cache_clear()
    worker_module._runner = None


async def _await_job(client, job_id, timeout=30.0):
    """Poll until the job leaves its active states."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        payload = (await client.get(f"/api/jobs/{job_id}")).json()
        if not payload["running"] and payload["status"] not in (
            "pending", "planning", "running"
        ):
            return payload
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {payload}")


# --------------------------------------------------------------------- basics


async def test_health_and_index(api):
    assert (await api.get("/healthz")).json()["status"] == "ok"
    page = await api.get("/")
    assert page.status_code == 200
    assert "CloudTransfer" in page.text


async def test_status_lists_accounts_and_redirect_uris(api):
    payload = (await api.get("/api/status")).json()
    assert payload["configured"] == {"google": True, "microsoft": True}
    assert payload["google"]["email"] == "tester@example.com"
    assert payload["microsoft"]["label"] == "tester@contoso.example"
    assert payload["redirect_uris"]["google"].endswith("/auth/google/callback")
    assert payload["transfer"]["chunk_size"] == ONEDRIVE_CHUNK_MULTIPLE * 2


async def test_disconnecting_an_account_blocks_new_transfers(api, onedrive):
    assert (await api.post("/api/accounts/google/disconnect")).json()["disconnected"] is True
    assert (await api.get("/api/status")).json()["google"] is None

    blocked = await api.post(
        "/api/jobs",
        json={"source_folder_id": "root", "dest_folder_id": onedrive.root.id},
    )
    assert blocked.status_code == 409
    assert "not connected" in blocked.json()["detail"]

    # Disconnecting twice is harmless.
    assert (await api.post("/api/accounts/google/disconnect")).json()["disconnected"] is False


async def test_unknown_provider_is_rejected(api):
    assert (await api.get("/auth/nope/start")).status_code == 404


async def test_authorize_redirect_carries_the_right_scopes(api):
    response = await api.get("/auth/google/start", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert "accounts.google.com" in location
    # Full Drive access, since files travel both ways.
    assert "auth%2Fdrive+openid" in location
    assert "drive.readonly" not in location
    # Without these, Google issues no refresh token and the app dies in an hour.
    assert "access_type=offline" in location
    assert "prompt=consent" in location


async def test_microsoft_authorize_requests_offline_access(api):
    response = await api.get("/auth/microsoft/start", follow_redirects=False)
    location = response.headers["location"]
    assert "login.microsoftonline.com" in location
    assert "offline_access" in location
    assert "Files.ReadWrite" in location


async def test_callback_rejects_a_mismatched_state(api):
    """Guards against someone else's redirect being replayed at us."""
    response = await api.get("/auth/google/callback?code=abc&state=not-the-one")
    assert response.status_code == 400
    assert "state did not match" in response.json()["detail"]


# -------------------------------------------------------------------- browsing


async def test_browsing_drive_separates_folders_from_files(api, drive):
    drive.add_folder("Photos")
    drive.add_file("a.txt", b"hello")
    drive.add_native("Doc", "application/vnd.google-apps.document", {})

    payload = (await api.get("/api/browse/drive?parent=root")).json()
    assert [f["name"] for f in payload["folders"]] == ["Photos"]
    names = {f["name"]: f for f in payload["files"]}
    assert names["a.txt"]["needs_conversion"] is False
    assert names["Doc"]["needs_conversion"] is True  # the UI warns about this


async def test_browsing_onedrive_resolves_root(api, onedrive):
    await onedrive.ensure_folder(onedrive.root.id, "Target")
    payload = (await api.get("/api/browse/onedrive?parent=root")).json()
    assert payload["parent"]["id"] == onedrive.root.id
    assert [f["name"] for f in payload["folders"]] == ["Target"]


async def test_creating_a_destination_folder_sanitizes_the_name(api, onedrive):
    response = await api.post(
        f"/api/browse/onedrive/folder?parent={onedrive.root.id}&name=bad%3Aname"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["folder"]["name"] == "bad_name"
    assert payload["renamed_because"]


async def test_quota_is_exposed(api, onedrive):
    payload = (await api.get("/api/browse/onedrive/quota")).json()
    assert payload["remaining"] == onedrive.quota_remaining


# ------------------------------------------------------------------ transfers


async def test_a_job_runs_end_to_end_over_http(api, drive, onedrive, payload_factory):
    folder = drive.add_folder("Docs")
    big = payload_factory(ONEDRIVE_CHUNK_MULTIPLE * 3)
    drive.add_file("readme.txt", b"hello world")
    drive.add_file("data.bin", big, parent=folder.id)

    created = await api.post(
        "/api/jobs",
        json={
            "source_folder_id": "root",
            "source_folder_name": "My Drive",
            "dest_folder_id": onedrive.root.id,
            "dest_folder_path": "OneDrive",
            "conflict_policy": "rename",
            "verify": True,
            "start_immediately": True,
        },
    )
    assert created.status_code == 200
    job_id = created.json()["id"]

    final = await _await_job(api, job_id)
    assert final["status"] == "completed"
    assert final["done_files"] == 2
    assert onedrive.tree() == {"readme.txt": b"hello world", "Docs/data.bin": big}


async def test_task_listing_shows_renames_and_verification(api, drive, onedrive):
    drive.add_file("awkward:name.txt", b"x" * 100)

    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)

    payload = (await api.get(f"/api/jobs/{job_id}/tasks")).json()
    assert payload["total"] == 1
    task = payload["tasks"][0]
    assert task["dest_name"] == "awkward_name.txt"
    assert task["renamed"] is True
    assert task["rename_notes"]
    assert task["verified"] is True


async def test_the_csv_report_covers_every_outcome(api, drive, onedrive):
    drive.add_file("good.txt", b"fine")
    drive.add_native("Survey", "application/vnd.google-apps.form", {})
    drive.add_file("blocked.txt", b"nope")
    onedrive.fail_permanently.add("blocked.txt")

    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    final = await _await_job(api, job_id)
    assert final["status"] == "completed_with_errors"

    report = await api.get(f"/api/jobs/{job_id}/report?fmt=csv")
    assert report.status_code == 200
    assert "text/csv" in report.headers["content-type"]
    assert "attachment" in report.headers["content-disposition"]
    body = report.text
    assert "good.txt" in body and "done" in body
    assert "Google Forms" in body       # the skip reason
    assert "access denied" in body      # the failure reason

    as_json = (await api.get(f"/api/jobs/{job_id}/report?fmt=json")).json()
    assert {t["status"] for t in as_json["tasks"]} == {"done", "skipped", "failed"}


async def test_failed_files_can_be_requeued(api, drive, onedrive):
    drive.add_file("blocked.txt", b"payload")
    onedrive.fail_permanently.add("blocked.txt")

    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)

    onedrive.fail_permanently.clear()
    response = await api.post(f"/api/jobs/{job_id}/retry-failed")
    assert response.json()["requeued"] == 1

    final = await _await_job(api, job_id)
    assert final["status"] == "completed"
    assert onedrive.tree() == {"blocked.txt": b"payload"}


async def test_preflight_warns_when_the_destination_is_too_full(api, onedrive):
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": False,
            },
        )
    ).json()["id"]

    # Give the job a known size without moving anything, then shrink the quota.
    async with session_scope(get_settings()) as session:
        job = await session.get(Job, job_id)
        job.total_bytes = 5_000_000
        job.done_bytes = 0
    onedrive.quota_remaining = 1000

    payload = (await api.get(f"/api/jobs/{job_id}/preflight")).json()
    assert payload["bytes_to_move"] == 5_000_000
    assert payload["destination_remaining"] == 1000
    assert payload["fits"] is False
    assert "only 1000 bytes free" in payload["warning"]


async def test_preflight_passes_when_there_is_room(api, onedrive):
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": False,
            },
        )
    ).json()["id"]
    payload = (await api.get(f"/api/jobs/{job_id}/preflight")).json()
    assert payload["fits"] is True
    assert payload["warning"] is None


async def test_jobs_can_be_deleted_but_not_while_running(api, drive, onedrive):
    drive.add_file("a.txt", b"a")
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)

    assert (await api.delete(f"/api/jobs/{job_id}")).json()["deleted"] == job_id
    assert (await api.get(f"/api/jobs/{job_id}")).status_code == 404


async def test_missing_jobs_report_404(api):
    assert (await api.get("/api/jobs/9999")).status_code == 404
    assert (await api.get("/api/jobs/9999/tasks")).status_code == 404
    assert (await api.post("/api/jobs/9999/start")).status_code == 404
    assert (await api.delete("/api/jobs/9999")).status_code == 404


async def test_pausing_an_idle_job_is_a_conflict(api, drive, onedrive):
    drive.add_file("a.txt", b"a")
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)
    assert (await api.post(f"/api/jobs/{job_id}/pause")).status_code == 409


async def test_progress_events_stream_and_terminate(api, drive, onedrive):
    drive.add_file("a.txt", b"a" * 50)
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)

    # On a finished job the stream emits one frame and closes.
    events: list[str] = []
    async with api.stream("GET", f"/api/jobs/{job_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(line)
                break
    assert events and '"status": "completed"' in events[0]


# ---------------------------------------------------------------- setup API


async def test_setup_reports_what_is_missing_and_where_to_point_it(api, monkeypatch):
    """A fresh instance with no environment credentials: the desktop case."""
    await api.delete("/api/setup/google")
    await api.delete("/api/setup/microsoft")
    monkeypatch.delenv("CT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CT_MS_CLIENT_ID", raising=False)
    get_settings.cache_clear()

    payload = (await api.get("/api/setup")).json()
    assert payload["complete"] is False
    google = payload["providers"]["google"]
    assert google["configured"] is False
    assert google["name"] == "Google Drive"
    # The exact value to register, so nothing has to be guessed.
    assert google["redirect_uri"].endswith("/auth/google/callback")
    assert payload["providers"]["microsoft"]["redirect_uri"].endswith(
        "/auth/microsoft/callback"
    )


async def test_saving_credentials_through_the_api(api):
    response = await api.post(
        "/api/setup/microsoft",
        json={"client_id": "11111111-2222-3333-4444-555555555555", "client_secret": ""},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["public_client"] is True  # a desktop registration has no secret
    assert body["has_secret"] is False

    payload = (await api.get("/api/setup")).json()
    assert payload["providers"]["microsoft"]["configured"] is True
    assert payload["providers"]["microsoft"]["source"] == "stored"
    assert payload["providers"]["microsoft"]["client_id"].startswith("11111111")


async def test_a_saved_secret_is_never_returned(api):
    """The setup page must be able to show state without leaking the secret."""
    await api.post(
        "/api/setup/google",
        json={"client_id": "gid.apps.googleusercontent.com", "client_secret": "GOCSPX-abc"},
    )
    body = (await api.get("/api/setup")).text
    assert "GOCSPX-abc" not in body
    assert (await api.get("/api/setup")).json()["providers"]["google"]["has_secret"] is True


async def test_an_empty_client_id_is_rejected(api):
    response = await api.post(
        "/api/setup/google", json={"client_id": "   ", "client_secret": ""}
    )
    assert response.status_code in (400, 422)


async def test_setup_rejects_an_unknown_provider(api):
    assert (
        await api.post("/api/setup/dropbox", json={"client_id": "x"})
    ).status_code == 404
    assert (await api.delete("/api/setup/dropbox")).status_code == 404


async def test_clearing_credentials_through_the_api(api):
    await api.post("/api/setup/microsoft", json={"client_id": "mid", "client_secret": ""})
    assert (await api.delete("/api/setup/microsoft")).json()["cleared"] is True
    # Falls back to the environment, which this fixture sets.
    assert (await api.get("/api/setup")).json()["providers"]["microsoft"]["source"] == "env"


async def test_connecting_is_refused_before_setup(api, monkeypatch):
    await api.delete("/api/setup/google")
    monkeypatch.delenv("CT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CT_GOOGLE_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()

    response = await api.get("/auth/google/start", follow_redirects=False)
    assert response.status_code == 400
    assert "not set up" in response.json()["detail"]


async def test_the_authorize_redirect_uses_the_stored_application(api):
    await api.post(
        "/api/setup/google",
        json={"client_id": "stored-client-id", "client_secret": "s"},
    )
    response = await api.get("/auth/google/start", follow_redirects=False)
    assert response.status_code == 307

    location = httpx.URL(response.headers["location"])
    assert location.params["client_id"] == "stored-client-id"
    # PKCE must be present: the desktop build ships its client ID publicly.
    assert location.params["code_challenge_method"] == "S256"
    assert location.params["code_challenge"]


async def test_redirect_uris_follow_the_address_actually_used(api):
    """The desktop build may land on a different port; the page must say so."""
    payload = (await api.get("/api/setup", headers={"Host": "127.0.0.1:9999"})).json()
    assert "127.0.0.1:9999" in payload["providers"]["google"]["redirect_uri"]


async def test_an_explicit_base_url_wins_over_the_request(api, monkeypatch):
    """Behind a proxy the request arrives as http; the configured address rules."""
    monkeypatch.setenv("CT_BASE_URL", "https://transfer.example.com")
    get_settings.cache_clear()
    payload = (await api.get("/api/setup", headers={"Host": "127.0.0.1:9999"})).json()
    assert payload["providers"]["google"]["redirect_uri"] == (
        "https://transfer.example.com/auth/google/callback"
    )


# -------------------------------------------------------- direction guards


async def test_a_reverse_job_is_refused_on_a_read_only_google_grant(api):
    """No refresh widens a scope, so this has to be caught before any upload."""
    from sqlalchemy import select

    from cloudtransfer.db import session_scope
    from cloudtransfer.models import Account
    from cloudtransfer.oauth import GOOGLE_READONLY_SCOPE

    async with session_scope(get_settings()) as session:
        account = (
            await session.execute(select(Account).where(Account.provider == "google"))
        ).scalar_one()
        account.scope = f"{GOOGLE_READONLY_SCOPE} openid email"

    response = await api.post(
        "/api/jobs",
        json={
            "direction": "onedrive_to_gdrive",
            "source_folder_id": "root",
            "dest_folder_id": "root",
        },
    )
    assert response.status_code == 409
    assert "read-only" in response.json()["detail"]
    assert "connect it again" in response.json()["detail"]


async def test_the_forward_direction_still_works_on_a_read_only_grant(api, drive, onedrive):
    """Sending files to OneDrive never needed write access to Drive."""
    from sqlalchemy import select

    from cloudtransfer.db import session_scope
    from cloudtransfer.models import Account
    from cloudtransfer.oauth import GOOGLE_READONLY_SCOPE

    async with session_scope(get_settings()) as session:
        account = (
            await session.execute(select(Account).where(Account.provider == "google"))
        ).scalar_one()
        account.scope = f"{GOOGLE_READONLY_SCOPE} openid email"

    drive.add_file("a.txt", b"hello")
    response = await api.post(
        "/api/jobs",
        json={
            "direction": "gdrive_to_onedrive",
            "source_folder_id": "root",
            "dest_folder_id": onedrive.root.id,
            "start_immediately": True,
        },
    )
    assert response.status_code == 200
    final = await _await_job(api, response.json()["id"])
    assert final["status"] == "completed"


async def test_status_reports_whether_drive_can_be_written(api):
    payload = (await api.get("/api/status")).json()
    assert payload["google"]["can_write"] is True
    # Microsoft is always read/write; the flag is there for a uniform shape.
    assert payload["microsoft"]["can_write"] is True


async def test_a_reverse_job_records_its_direction(api, drive, onedrive):
    onedrive.add_file("from-onedrive.txt", b"payload")
    created = await api.post(
        "/api/jobs",
        json={
            "direction": "onedrive_to_gdrive",
            "source_folder_id": "root",
            "dest_folder_id": drive.root.id,
            "start_immediately": True,
        },
    )
    assert created.status_code == 200
    assert created.json()["direction"] == "onedrive_to_gdrive"
    assert created.json()["direction_label"] == "OneDrive to Google Drive"

    final = await _await_job(api, created.json()["id"])
    assert final["status"] == "completed"
    assert drive.tree() == {"from-onedrive.txt": b"payload"}


# ---------------------------------------------------------- ticked transfers


async def test_only_ticked_items_are_transferred(api, drive, onedrive):
    folder = drive.add_folder("Wanted")
    drive.add_file("inside.txt", b"inside", parent=folder.id)
    loose = drive.add_file("loose.txt", b"loose")
    drive.add_file("untouched.txt", b"untouched")
    drive.add_folder("Untouched folder")

    created = await api.post(
        "/api/jobs",
        json={
            "source_folder_id": "root",
            "dest_folder_id": onedrive.root.id,
            "selection": [folder.id, loose.id],
            "start_immediately": True,
        },
    )
    assert created.status_code == 200
    assert created.json()["selection_count"] == 2

    final = await _await_job(api, created.json()["id"])
    assert final["status"] == "completed"
    assert onedrive.tree() == {
        "Wanted/inside.txt": b"inside",
        "loose.txt": b"loose",
    }


async def test_no_selection_still_moves_everything(api, drive, onedrive):
    drive.add_file("a.txt", b"a")
    drive.add_file("b.txt", b"b")

    created = await api.post(
        "/api/jobs",
        json={
            "source_folder_id": "root",
            "dest_folder_id": onedrive.root.id,
            "start_immediately": True,
        },
    )
    assert created.json()["selection_count"] == 0
    await _await_job(api, created.json()["id"])
    assert set(onedrive.tree()) == {"a.txt", "b.txt"}


# ------------------------------------------------------------ stop and delete


async def test_a_running_transfer_can_be_deleted(api, drive, onedrive, payload_factory):
    """Refusing this left people stuck exactly when stopping was the point."""
    for i in range(6):
        drive.add_file(f"big{i}.bin", payload_factory(1_310_720 * 2))
    onedrive.chunk_delay = 0.02

    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]

    for _ in range(600):
        if (await api.get(f"/api/jobs/{job_id}")).json()["running"]:
            break
        await asyncio.sleep(0.01)

    response = await api.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["stopped"] is True
    assert (await api.get(f"/api/jobs/{job_id}")).status_code == 404


async def test_deleting_a_finished_transfer_reports_it_was_not_running(
    api, drive, onedrive
):
    drive.add_file("a.txt", b"a")
    job_id = (
        await api.post(
            "/api/jobs",
            json={
                "source_folder_id": "root",
                "dest_folder_id": onedrive.root.id,
                "start_immediately": True,
            },
        )
    ).json()["id"]
    await _await_job(api, job_id)

    response = await api.delete(f"/api/jobs/{job_id}")
    assert response.json() == {"deleted": job_id, "stopped": False}
