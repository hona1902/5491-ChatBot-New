"""Tests for normal-user Notebook Chat stuck loop fix.

Covers the 10 test cases specified in the bug report:
1.  Normal authenticated user can call POST /api/chat/context for an accessible notebook.
2.  Unauthenticated POST /api/chat/context is rejected.
3.  Normal user cannot build context for an inaccessible (missing) notebook.
4.  Normal authenticated user can GET /api/sources?notebook_id=accessible_notebook.
5.  Inaccessible notebook returns 403/404 from GET /api/sources.
6.  Admin GET /api/sources?notebook_id still works.
7.  Frontend useNotebookSources staleTime is >= 30 seconds and refetchOnWindowFocus is False.
8.  useNotebookChat does not re-call buildContext on every sources refetch (cycle guard).
9.  Streaming message remains visible after refetchCurrentSession returns empty.
10. Source Chat regression: DELETE /sources still requires admin.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.auth import create_access_token


# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """Create test client after environment variables have been cleared by conftest."""
    from api.main import app

    return TestClient(app)


def _token(role="user", user_id="app_user:normal1"):
    return create_access_token({"sub": user_id, "role": role})


def _auth(role="user"):
    return {"Authorization": f"Bearer {_token(role)}"}


def _mock_user(role="user", is_active=True):
    from open_notebook.domain.user import AppUser

    return AppUser(
        id="app_user:normal1",
        username="normaluser",
        email="normal@example.com",
        role=role,
        is_active=is_active,
    )


# ── 1. Normal user can POST /api/chat/context for accessible notebook ─────────


class TestBuildContextAuth:
    """POST /chat/context must require authentication and work for normal users."""

    @patch("api.auth.AppUser")
    @patch("api.routers.chat.repo_query", new_callable=AsyncMock)
    def test_normal_user_can_build_context(self, mock_repo_query, mock_auth_cls, client):
        """Test 1: Normal authenticated user can build context for accessible notebook."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))
        # repo_query returns the notebook row (notebook exists and is accessible)
        mock_repo_query.return_value = [{"id": "notebook:test123"}]

        response = client.post(
            "/api/chat/context",
            json={
                "notebook_id": "notebook:test123",
                "context_config": {"sources": {}, "notes": {}},
            },
            headers=_auth(role="user"),
        )

        # Should succeed, not 403/401/404
        assert response.status_code == 200, (
            f"Normal user should be able to build context. Got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert "token_count" in body
        assert "char_count" in body
        assert "context" in body

    def test_unauthenticated_build_context_is_rejected(self, client):
        """Test 2: Unauthenticated POST /api/chat/context must return 401."""
        response = client.post(
            "/api/chat/context",
            json={
                "notebook_id": "notebook:test123",
                "context_config": {"sources": {}, "notes": {}},
            },
            # No Authorization header
        )

        assert response.status_code == 401, (
            f"Unauthenticated request should return 401. Got {response.status_code}"
        )

    @patch("api.auth.AppUser")
    @patch("api.routers.chat.repo_query", new_callable=AsyncMock)
    def test_normal_user_cannot_build_context_for_missing_notebook(
        self, mock_repo_query, mock_auth_cls, client
    ):
        """Test 3: Normal user gets 404 for truly missing/inaccessible notebook."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))
        # repo_query returns empty — notebook truly does not exist
        mock_repo_query.return_value = []

        response = client.post(
            "/api/chat/context",
            json={
                "notebook_id": "notebook:ghost",
                "context_config": {"sources": {}, "notes": {}},
            },
            headers=_auth(role="user"),
        )

        assert response.status_code == 404, (
            f"Missing notebook should return 404. Got {response.status_code}"
        )


# ── 4-6. GET /api/sources?notebook_id access fix ─────────────────────────────


class TestGetSourcesNotebookAccess:
    """GET /sources?notebook_id=X must work for normal users with accessible notebooks."""

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_normal_user_can_get_sources_for_accessible_notebook(
        self, mock_repo_query, mock_auth_cls, client
    ):
        """Test 4: Normal authenticated user can GET sources for accessible notebook."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))

        def _repo_query_side_effect(query, params=None):
            # First call: notebook existence check → returns notebook row
            if "FROM notebook" in query or "from notebook" in query.lower():
                return [{"id": "notebook:h1nchslgac1pcad05fzm"}]
            # Second call: sources query → returns empty list (no sources)
            return []

        mock_repo_query.side_effect = _repo_query_side_effect

        response = client.get(
            "/api/sources?notebook_id=notebook:h1nchslgac1pcad05fzm",
            headers=_auth(role="user"),
        )

        assert response.status_code == 200, (
            f"Normal user with accessible notebook should get 200. "
            f"Got {response.status_code}: {response.text}"
        )
        assert isinstance(response.json(), list)

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_inaccessible_notebook_returns_404(
        self, mock_repo_query, mock_auth_cls, client
    ):
        """Test 5: Truly missing notebook returns 404 from GET /sources."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))
        # repo_query returns empty — notebook doesn't exist at all
        mock_repo_query.return_value = []

        response = client.get(
            "/api/sources?notebook_id=notebook:nonexistent",
            headers=_auth(role="user"),
        )

        assert response.status_code in (403, 404), (
            f"Missing notebook should return 403 or 404. Got {response.status_code}"
        )

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_admin_can_get_sources_for_notebook(
        self, mock_repo_query, mock_auth_cls, client
    ):
        """Test 6: Admin GET /sources?notebook_id still works as before."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="admin"))

        def _repo_query_side_effect(query, params=None):
            if "FROM notebook" in query or "from notebook" in query.lower():
                return [{"id": "notebook:admin_nb"}]
            return []

        mock_repo_query.side_effect = _repo_query_side_effect

        response = client.get(
            "/api/sources?notebook_id=notebook:admin_nb",
            headers=_auth(role="admin"),
        )

        assert response.status_code == 200, (
            f"Admin should be able to get sources. Got {response.status_code}"
        )


# ── 7. Frontend staleTime and refetchOnWindowFocus ────────────────────────────


class TestFrontendSourcesHookConfig:
    """Verify useNotebookSources has safe refetch settings to prevent loops."""

    def test_notebook_sources_staletime_is_at_least_30s(self):
        """Test 7a: useNotebookSources staleTime must be >= 30 seconds."""
        import re
        hook_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "lib", "hooks", "use-sources.ts"
        )
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the useInfiniteQuery block (useNotebookSources)
        # Extract staleTime value from useNotebookSources
        pattern = r"useInfiniteQuery\([\s\S]*?staleTime:\s*(\d+)\s*\*\s*1000"
        match = re.search(pattern, content)
        assert match, "Could not find staleTime in useNotebookSources"
        stale_seconds = int(match.group(1))
        assert stale_seconds >= 30, (
            f"useNotebookSources staleTime must be >= 30s to prevent refetch loop. "
            f"Found: {stale_seconds}s"
        )

    def test_notebook_sources_refetch_on_window_focus_is_false(self):
        """Test 7b: useNotebookSources refetchOnWindowFocus must be false."""
        hook_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "lib", "hooks", "use-sources.ts"
        )
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        import re
        # Extract the useNotebookSources function body
        func_pattern = r"export function useNotebookSources\([\s\S]*?(?=\nexport function|\Z)"
        func_match = re.search(func_pattern, content)
        assert func_match, "Could not find useNotebookSources function"
        func_body = func_match.group(0)

        # Find refetchOnWindowFocus in the function body
        focus_pattern = r"refetchOnWindowFocus:\s*(true|false)"
        focus_match = re.search(focus_pattern, func_body)
        assert focus_match, "refetchOnWindowFocus not set in useNotebookSources"
        assert focus_match.group(1) == "false", (
            "useNotebookSources refetchOnWindowFocus must be false to prevent loop"
        )


# ── 8. useNotebookChat cycle guard ────────────────────────────────────────────


class TestNotebookChatCycleGuard:
    """Verify useNotebookChat uses a stable key to guard buildContext calls."""

    def test_usenotebookchat_uses_context_key_not_build_context_dep(self):
        """Test 8: useNotebookChat effect must depend on context key, not buildContext."""
        hook_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "lib", "hooks", "useNotebookChat.ts"
        )
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The fix replaces [buildContext] dep with [currentContextKey]
        assert "currentContextKey" in content, (
            "useNotebookChat must use currentContextKey to guard buildContext calls"
        )
        assert "serializeContextSelections" in content, (
            "useNotebookChat must serialize context selections for stable comparison"
        )
        # The old problematic pattern should not be present
        assert "], [buildContext])" not in content, (
            "useNotebookChat must NOT depend on [buildContext] directly in the "
            "token-count effect — this caused the infinite loop"
        )

    def test_streamed_messages_guard_present(self):
        """Test 9: refetchCurrentSession must guard against replacing messages with empty."""
        hook_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "lib", "hooks", "useNotebookChat.ts"
        )
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The fix wraps refetchCurrentSession in a .then() with a length guard
        assert "fetchedMessages.length > 0" in content, (
            "useNotebookChat must guard against replacing messages with empty "
            "refetchCurrentSession response"
        )


# ── 10. Source Chat regression: admin-only mutations still require admin ──────


class TestAdminOnlyMutationsStillProtected:
    """Source creation and deletion must still require admin after the fix."""

    @patch("api.auth.AppUser")
    def test_normal_user_cannot_create_source(self, mock_auth_cls, client):
        """Test 10a: POST /sources must still require admin role."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))

        response = client.post(
            "/api/sources",
            data={"type": "text", "content": "hello", "notebooks": '[]'},
            headers=_auth(role="user"),
        )

        assert response.status_code == 403, (
            f"Normal user should not be able to create sources. Got {response.status_code}"
        )

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    def test_normal_user_cannot_delete_source(self, mock_get, mock_auth_cls, client):
        """Test 10b: DELETE /sources/{id} must still require admin role."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))
        mock_source = MagicMock()
        mock_source.id = "source:test"
        mock_source.delete = AsyncMock(return_value=True)
        mock_get.return_value = mock_source

        response = client.delete(
            "/api/sources/source:test",
            headers=_auth(role="user"),
        )

        assert response.status_code == 403, (
            f"Normal user should not be able to delete sources. Got {response.status_code}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
