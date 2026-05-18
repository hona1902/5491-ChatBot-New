"""
Wave 2A-2 API Tests — Table Endpoints (tasks 4.6–4.11)
=======================================================
Tests for:
  4.6  GET /sources/{id}/tables → 200 with correct list (owner)
  4.7  GET /sources/{id}/tables → 401 when unauthenticated
  4.8  GET /sources/{id}/tables → 200 [] for source with no tables
  4.9  GET /sources/{id}/tables/{table_id} → pagination: offset+limit, total_rows
  4.10 GET /sources/{id}/tables/nonexistent → 404
  4.11 GET /sources/{id} → response includes table_count field

All tests run offline — no live DB or network required.
Auth is mocked via JWT tokens; DB calls are patched.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.auth import create_access_token


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


def _token(role="user", user_id="app_user:u1"):
    return create_access_token({"sub": user_id, "role": role})


def _auth(role="user"):
    return {"Authorization": f"Bearer {_token(role)}"}


def _mock_user(role="user", is_active=True):
    from open_notebook.domain.user import AppUser
    return AppUser(
        id="app_user:u1",
        username="testuser",
        email="test@example.com",
        role=role,
        is_active=is_active,
    )


def _mock_source(source_id="source:abc"):
    src = MagicMock()
    src.id = source_id
    src.title = "Test CSV"
    src.topics = []
    src.asset = None
    src.full_text = "col1,col2\n1,2"
    src.command = None
    src.created = "2024-01-01T00:00:00"
    src.updated = "2024-01-01T00:00:00"
    return src


def _table_row(idx=0, source_id="source:abc"):
    return {
        "id": f"source_table:t{idx}",
        "table_id": f"{source_id}_table_{idx}",
        "page_number": None,
        "sheet_name": f"Sheet{idx}",
        "title": None,
        "row_count": 3,
        "col_count": 2,
        "column_headers": ["A", "B"],
        "truncated": False,
    }


def _table_detail_row(source_id="source:abc"):
    rows = [{"A": str(i), "B": str(i * 2)} for i in range(10)]
    return {
        "id": "source_table:t0",
        "table_id": f"{source_id}_table_0",
        "page_number": None,
        "sheet_name": "Sheet1",
        "title": None,
        "row_count": 10,
        "col_count": 2,
        "column_headers": ["A", "B"],
        "truncated": False,
        "markdown_repr": "| A | B |\n|---|---|\n| 0 | 0 |",
        "row_data": rows,
    }


# ── Task 4.6 — GET /sources/{id}/tables returns 200 with correct list ─────────

class TestGetSourceTables:
    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_returns_200_with_table_list(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """4.6 Owner gets 200 with the correct list of SourceTableListItem."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = [_table_row(0), _table_row(1)]

        resp = client.get("/api/sources/source:abc/tables", headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["table_id"] == "source:abc_table_0"
        assert data[0]["col_count"] == 2
        assert data[0]["column_headers"] == ["A", "B"]
        assert data[0]["truncated"] is False

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_source_not_found_returns_404(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """GET /sources/unknown/tables → 404 when source missing."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = None

        resp = client.get("/api/sources/source:nope/tables", headers=_auth())

        assert resp.status_code == 404


# ── Task 4.7 — Unauthenticated request returns 401 ───────────────────────────

class TestTableAuthRBAC:
    def test_unauthenticated_returns_401(self, client):
        """4.7 No token → 401 (missing authorization)."""
        resp = client.get("/api/sources/source:abc/tables")
        assert resp.status_code == 401

    @patch("api.auth.AppUser")
    def test_inactive_user_returns_403(self, mock_auth_cls, client):
        """4.7 Inactive user → 403."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(is_active=False))
        resp = client.get(
            "/api/sources/source:abc/tables",
            headers=_auth(),
        )
        assert resp.status_code == 403

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_regular_user_can_read_tables(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Regular (non-admin) users can read tables — endpoint uses get_current_user."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user(role="user"))
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = []

        resp = client.get("/api/sources/source:abc/tables", headers=_auth(role="user"))

        assert resp.status_code == 200


# ── Task 4.8 — Source with no tables returns 200 [] ──────────────────────────

class TestSourceNoTables:
    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_no_tables_returns_empty_list(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """4.8 Source exists but has no source_table records → 200 []."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = []  # DB returns nothing

        resp = client.get("/api/sources/source:abc/tables", headers=_auth())

        assert resp.status_code == 200
        assert resp.json() == []


# ── Task 4.9 — Table detail: pagination ──────────────────────────────────────

class TestTableDetailPagination:
    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_pagination_offset_limit_respected(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """4.9 offset+limit respected; total_rows reflects full count."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = [_table_detail_row()]  # 10 rows in row_data

        resp = client.get(
            "/api/sources/source:abc/tables/source_table:t0",
            params={"offset": 3, "limit": 4},
            headers=_auth(),
        )

        assert resp.status_code == 200
        data = resp.json()
        # total_rows = 10 (from row_count in DB record)
        assert data["total_rows"] == 10
        # rows slice: [3:7] → 4 rows
        assert len(data["rows"]) == 4
        assert data["offset"] == 3
        assert data["limit"] == 4

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_default_limit_200(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Default limit is 200; all rows returned when < 200."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = [_table_detail_row()]  # 10 rows

        resp = client.get(
            "/api/sources/source:abc/tables/source_table:t0",
            headers=_auth(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 200
        assert len(data["rows"]) == 10  # all 10 fit within default limit

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_offset_beyond_end_returns_empty_rows(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Offset past the last row → rows=[], total_rows still correct."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = [_table_detail_row()]  # 10 rows

        resp = client.get(
            "/api/sources/source:abc/tables/source_table:t0",
            params={"offset": 999, "limit": 50},
            headers=_auth(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"] == []
        assert data["total_rows"] == 10

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_response_includes_metadata_fields(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Detail response includes id, table_id, column_headers, markdown_repr."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = [_table_detail_row()]

        resp = client.get(
            "/api/sources/source:abc/tables/source_table:t0",
            headers=_auth(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "source_table:t0"
        assert data["column_headers"] == ["A", "B"]
        assert "markdown_repr" in data
        assert data["col_count"] == 2


# ── Task 4.10 — Unknown table_id returns 404 ─────────────────────────────────

class TestTableNotFound:
    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_unknown_table_returns_404(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """4.10 GET /sources/{id}/tables/nonexistent → 404."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = _mock_source()
        mock_query.return_value = []  # table not found

        resp = client.get(
            "/api/sources/source:abc/tables/source_table:nope",
            headers=_auth(),
        )

        assert resp.status_code == 404

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_unknown_source_in_table_detail_returns_404(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """Table detail → 404 when source itself is missing."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_source_get.return_value = None  # source not found

        resp = client.get(
            "/api/sources/source:nope/tables/source_table:t0",
            headers=_auth(),
        )

        assert resp.status_code == 404


# ── Task 4.11 — GET /sources/{id} includes table_count ───────────────────────

class TestSourceDetailTableCount:
    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_source_detail_includes_table_count(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """4.11 GET /sources/{id} response includes table_count field."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())

        src = _mock_source()
        src.get_embedded_chunks = AsyncMock(return_value=5)
        src.get_status = AsyncMock(return_value=None)
        src.get_processing_progress = AsyncMock(return_value=None)
        mock_source_get.return_value = src

        # Combined meta query returns notebooks + table_count
        mock_query.return_value = [{"notebooks": [], "table_count": 3}]

        resp = client.get("/api/sources/source:abc", headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        assert "table_count" in data
        assert data["table_count"] == 3

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_source_detail_table_count_zero_when_no_tables(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """table_count is 0 when source has no source_table records."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())

        src = _mock_source()
        src.get_embedded_chunks = AsyncMock(return_value=0)
        src.get_status = AsyncMock(return_value=None)
        src.get_processing_progress = AsyncMock(return_value=None)
        mock_source_get.return_value = src

        mock_query.return_value = [{"notebooks": [], "table_count": 0}]

        resp = client.get("/api/sources/source:abc", headers=_auth())

        assert resp.status_code == 200
        assert resp.json()["table_count"] == 0

    @patch("api.auth.AppUser")
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    def test_table_count_not_present_in_list_endpoint(
        self, mock_query, mock_source_get, mock_auth_cls, client
    ):
        """table_count is NOT a field on the source list response (only on detail)."""
        mock_auth_cls.get = AsyncMock(return_value=_mock_user())
        mock_query.return_value = []

        resp = client.get("/api/sources", headers=_auth())

        assert resp.status_code == 200
        # List items don't have table_count (it's a detail-only field)
        for item in resp.json():
            assert "table_count" not in item


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
