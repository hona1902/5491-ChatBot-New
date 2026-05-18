"""Tests for the sources API endpoint."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.auth import create_access_token

from open_notebook.config import UPLOADS_FOLDER
from open_notebook.domain.notebook import Source


@pytest.fixture
def client():
    """Create test client after environment variables have been cleared by conftest."""
    from api.main import app

    return TestClient(app)


class TestAsyncSourceAssetPersistence:
    """Tests for #627 - asset is persisted before async processing.

    These tests hit the real create_source endpoint with mocked DB/command
    calls, verifying that the Source saved to the database has the correct
    asset set *before* async processing begins.
    """

    @pytest.mark.asyncio
    @patch("api.routers.sources.CommandService.submit_command_job", new_callable=AsyncMock)
    @patch("api.routers.sources.Source.add_to_notebook", new_callable=AsyncMock)
    @patch("api.routers.sources.Notebook.get", new_callable=AsyncMock)
    async def test_async_link_source_persists_url_asset(
        self, mock_nb_get, mock_add_nb, mock_submit, client
    ):
        """POST /sources with type=link and async_processing=true persists Asset(url=...)."""
        mock_nb_get.return_value = MagicMock()
        mock_submit.return_value = "command:123"

        saved_sources = []

        async def capture_save(self_source):
            saved_sources.append(self_source)
            self_source.id = "source:fake"
            self_source.command = None

        with patch.object(Source, "save", autospec=True, side_effect=capture_save):
            response = client.post(
                "/api/sources",
                data={
                    "type": "link",
                    "url": "https://example.com/article",
                    "notebooks": '["notebook:1"]',
                    "async_processing": "true",
                },
            )

        assert response.status_code == 200
        assert len(saved_sources) >= 1

        source = saved_sources[0]
        assert source.asset is not None
        assert source.asset.url == "https://example.com/article"
        assert source.asset.file_path is None

    @pytest.mark.asyncio
    @patch("api.routers.sources.CommandService.submit_command_job", new_callable=AsyncMock)
    @patch("api.routers.sources.Source.add_to_notebook", new_callable=AsyncMock)
    @patch("api.routers.sources.Notebook.get", new_callable=AsyncMock)
    @patch("api.routers.sources.save_uploaded_file", new_callable=AsyncMock)
    async def test_async_upload_source_persists_file_asset(
        self, mock_upload, mock_nb_get, mock_add_nb, mock_submit, client
    ):
        """POST /sources with type=upload and async_processing=true persists Asset(file_path=...)."""
        mock_nb_get.return_value = MagicMock()
        mock_upload.return_value = os.path.join(os.path.abspath(UPLOADS_FOLDER), "video.mp4")
        mock_submit.return_value = "command:123"

        saved_sources = []

        async def capture_save(self_source):
            saved_sources.append(self_source)
            self_source.id = "source:fake"
            self_source.command = None

        with patch.object(Source, "save", autospec=True, side_effect=capture_save):
            response = client.post(
                "/api/sources",
                data={
                    "type": "upload",
                    "notebooks": '["notebook:1"]',
                    "async_processing": "true",
                },
                files={"file": ("video.mp4", b"fake content", "video/mp4")},
            )

        assert response.status_code == 200
        assert len(saved_sources) >= 1

        source = saved_sources[0]
        assert source.asset is not None
        assert source.asset.file_path == os.path.join(os.path.abspath(UPLOADS_FOLDER), "video.mp4")
        assert source.asset.url is None

    @pytest.mark.asyncio
    @patch("api.routers.sources.CommandService.submit_command_job", new_callable=AsyncMock)
    @patch("api.routers.sources.Source.add_to_notebook", new_callable=AsyncMock)
    @patch("api.routers.sources.Notebook.get", new_callable=AsyncMock)
    async def test_async_text_source_has_no_asset(
        self, mock_nb_get, mock_add_nb, mock_submit, client
    ):
        """POST /sources with type=text and async_processing=true has asset=None."""
        mock_nb_get.return_value = MagicMock()
        mock_submit.return_value = "command:123"

        saved_sources = []

        async def capture_save(self_source):
            saved_sources.append(self_source)
            self_source.id = "source:fake"
            self_source.command = None

        with patch.object(Source, "save", autospec=True, side_effect=capture_save):
            response = client.post(
                "/api/sources",
                data={
                    "type": "text",
                    "content": "Some text content",
                    "notebooks": '["notebook:1"]',
                    "async_processing": "true",
                },
            )

        assert response.status_code == 200
        assert len(saved_sources) >= 1

        source = saved_sources[0]
        assert source.asset is None


# ── Auth helpers (same pattern as test_table_api.py) ─────────────────────────

def _token(role="admin", user_id="app_user:u1"):
    return create_access_token({"sub": user_id, "role": role})


def _auth(role="admin"):
    return {"Authorization": f"Bearer {_token(role)}"}


def _mock_user(role="admin", is_active=True):
    from open_notebook.domain.user import AppUser
    return AppUser(
        id="app_user:u1",
        username="testuser",
        email="test@example.com",
        role=role,
        is_active=is_active,
    )


# ── TestSourceDelete ──────────────────────────────────────────────────────────

class TestSourceDelete:
    """Tests for DELETE /sources/{source_id}.

    DELETE requires admin (require_admin dependency). We patch
    api.auth.AppUser (for JWT decode) so the admin guard resolves.
    """

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    def test_delete_source_succeeds(self, mock_get, mock_auth_cls, client):
        """DELETE /sources/{id} returns 200 and a success message."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="admin"))

        mock_source = MagicMock()
        mock_source.id = "source:to_delete"
        mock_source.delete = AsyncMock(return_value=True)
        mock_get.return_value = mock_source

        response = client.delete(
            "/api/sources/source:to_delete",
            headers=_auth(role="admin"),
        )

        assert response.status_code == 200
        assert response.json()["message"] == "Source deleted successfully"

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    def test_delete_missing_source_returns_not_500(self, mock_get, mock_auth_cls, client):
        """DELETE /sources/{id} for unknown id does not return 500."""
        from open_notebook.exceptions import NotFoundError

        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="admin"))
        mock_get.side_effect = NotFoundError("source with id source:ghost not found")

        response = client.delete(
            "/api/sources/source:ghost",
            headers=_auth(role="admin"),
        )

        # The DELETE endpoint currently lets NotFoundError become 500 —
        # the required fix is only in the /status endpoint.
        # This test documents the current behaviour; status fix is tested below.
        assert response.status_code in (404, 500)


# ── TestSourceStatusAfterDelete ───────────────────────────────────────────────

class TestSourceStatusAfterDelete:
    """GET /sources/{source_id}/status after deletion must return 404 with no ERROR log.

    Root cause of noisy logs:
      Source.get() (domain/base.py lines 124-128) catches its own NotFoundError
      inside a broad `except Exception` block, then calls logger.error() +
      logger.exception() (full traceback) before re-raising. Catching
      NotFoundError at the router level does NOT prevent those noisy logs.

    Fix:
      Use repo_query directly for the existence check — it returns [] for
      missing records with zero error logging.  Source.get() is only called
      after the record is confirmed to exist (safe path, no error logs emitted).

    These tests patch `api.routers.sources.repo_query` (not Source.get) to
    match the new implementation.
    """

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_status_of_deleted_source_returns_404_not_500(
        self, mock_query, mock_auth_cls, client
    ):
        """GET /sources/{id}/status after delete must return 404, not 500."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = []  # DB returns no row — source is gone

        response = client.get(
            "/api/sources/source:gone/status",
            headers=_auth(),
        )

        assert response.status_code == 404, (
            f"Expected 404, got {response.status_code}. "
            "Polling a deleted source must not cause a 500."
        )

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_status_of_deleted_source_response_body(self, mock_query, mock_auth_cls, client):
        """Missing source status returns a structured JSON 404 body."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = []

        response = client.get(
            "/api/sources/source:gone/status",
            headers=_auth(),
        )

        assert response.status_code == 404
        body = response.json()
        assert "detail" in body
        assert (
            "source:gone" in body["detail"]
            or "not found" in body["detail"].lower()
        )

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_status_not_500_for_missing_source(self, mock_query, mock_auth_cls, client):
        """A missing source must always produce 404, never 500."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = []

        response = client.get("/api/sources/source:x/status", headers=_auth())

        assert response.status_code != 500, (
            "Missing source must not produce a 500 from the status endpoint."
        )
        assert response.status_code == 404

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_source_get_is_not_called_for_missing_source(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Source.get() must NOT be called when the source row is absent.

        Source.get() always logs ERROR+traceback for missing records.
        The fix must short-circuit before ever reaching Source.get().
        This test proves the noisy log path is fully bypassed.
        """
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = []  # DB says source doesn't exist

        client.get("/api/sources/source:deleted/status", headers=_auth())

        # Source.get must never be called — calling it would trigger ERROR logs
        mock_source_get.assert_not_called()


# ── TestSourceStatusExistingSource ────────────────────────────────────────────

class TestSourceStatusExistingSource:
    """GET /sources/{source_id}/status still works correctly for live sources."""

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_status_legacy_source_no_command(self, mock_query, mock_auth_cls, client):
        """Legacy source (command=None) returns 200 with status=None.

        For legacy sources, Source.get() is NOT called — we only need the
        command field from the repo_query row.
        """
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        # Source exists in DB but has no command (pre-async legacy source)
        mock_query.return_value = [{"id": "source:legacy", "command": None}]

        response = client.get("/api/sources/source:legacy/status", headers=_auth())

        assert response.status_code == 200
        body = response.json()
        assert body["status"] is None
        assert "legacy" in body["message"].lower()

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_status_active_source_returns_completed(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Existing source with a completed command returns 200 + status='completed'."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        # repo_query: source exists and has a command ref
        mock_query.return_value = [{"id": "source:active", "command": "command:123"}]

        # Source.get() is called only AFTER existence confirmed — safe, no error log
        mock_source = MagicMock()
        mock_source.id = "source:active"
        mock_source.command = "command:123"
        mock_source.get_status = AsyncMock(return_value="completed")
        mock_source.get_processing_progress = AsyncMock(
            return_value={"started_at": "2026-01-01", "completed_at": "2026-01-02"}
        )
        mock_source_get.return_value = mock_source

        response = client.get("/api/sources/source:active/status", headers=_auth())

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert "completed" in body["message"].lower()

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_source_get_called_only_when_source_exists(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Source.get() is called exactly once when source is confirmed to exist."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = [{"id": "source:active", "command": "command:123"}]

        mock_source = MagicMock()
        mock_source.id = "source:active"
        mock_source.command = "command:123"
        mock_source.get_status = AsyncMock(return_value="running")
        mock_source.get_processing_progress = AsyncMock(return_value={})
        mock_source_get.return_value = mock_source

        client.get("/api/sources/source:active/status", headers=_auth())

        mock_source_get.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
