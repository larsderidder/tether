"""Tests for workspace disk usage tracking and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tether.workspace import (
    cleanup_orphan_workspace,
    dir_size_bytes,
    find_orphan_workspaces,
    list_workspace_usage,
)


# ---------------------------------------------------------------------------
# dir_size_bytes
# ---------------------------------------------------------------------------


class TestDirSizeBytes:
    def test_empty_directory(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert dir_size_bytes(str(d)) == 0

    def test_single_file(self, tmp_path):
        d = tmp_path / "ws"
        d.mkdir()
        f = d / "file.txt"
        f.write_bytes(b"hello world")  # 11 bytes
        size = dir_size_bytes(str(d))
        assert size == 11

    def test_multiple_files(self, tmp_path):
        d = tmp_path / "ws"
        d.mkdir()
        (d / "a.txt").write_bytes(b"aa")
        (d / "b.txt").write_bytes(b"bbb")
        (d / "sub").mkdir()
        (d / "sub" / "c.txt").write_bytes(b"cccc")
        size = dir_size_bytes(str(d))
        assert size == 2 + 3 + 4

    def test_nonexistent_directory_returns_zero(self, tmp_path):
        size = dir_size_bytes(str(tmp_path / "nonexistent"))
        assert size == 0

    def test_nested_directories(self, tmp_path):
        d = tmp_path / "ws"
        (d / "a" / "b").mkdir(parents=True)
        (d / "a" / "b" / "deep.txt").write_bytes(b"x" * 100)
        size = dir_size_bytes(str(d))
        assert size == 100


# ---------------------------------------------------------------------------
# list_workspace_usage
# ---------------------------------------------------------------------------


class TestListWorkspaceUsage:
    def test_returns_empty_when_no_workspace_root(self, tmp_path):
        with patch("tether.workspace.managed_workspaces_dir", return_value=str(tmp_path / "missing")):
            result = list_workspace_usage()
        assert result == []

    def test_returns_empty_when_root_has_no_dirs(self, tmp_path):
        root = tmp_path / "workspaces"
        root.mkdir()
        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            result = list_workspace_usage()
        assert result == []

    def test_lists_workspace_dirs(self, tmp_path):
        root = tmp_path / "workspaces"
        ws1 = root / "sess_aaa"
        ws1.mkdir(parents=True)
        (ws1 / "file.txt").write_bytes(b"a" * 1000)

        ws2 = root / "sess_bbb"
        ws2.mkdir()
        (ws2 / "file.txt").write_bytes(b"b" * 500)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            result = list_workspace_usage()

        assert len(result) == 2
        ids = {r["session_id"] for r in result}
        assert ids == {"sess_aaa", "sess_bbb"}

    def test_sorted_by_size_descending(self, tmp_path):
        root = tmp_path / "workspaces"
        for name, size in (("sess_big", 9000), ("sess_small", 100), ("sess_mid", 500)):
            ws = root / name
            ws.mkdir(parents=True)
            (ws / "f").write_bytes(b"x" * size)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            result = list_workspace_usage()

        sizes = [r["size_bytes"] for r in result]
        assert sizes == sorted(sizes, reverse=True)

    def test_entry_has_expected_keys(self, tmp_path):
        root = tmp_path / "workspaces"
        ws = root / "sess_xyz"
        ws.mkdir(parents=True)
        (ws / "f").write_bytes(b"hello")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            result = list_workspace_usage()

        assert len(result) == 1
        entry = result[0]
        assert entry["session_id"] == "sess_xyz"
        assert entry["path"] == str(ws)
        assert entry["size_bytes"] == 5

    def test_ignores_files_in_root(self, tmp_path):
        root = tmp_path / "workspaces"
        root.mkdir()
        (root / "stray.txt").write_bytes(b"ignored")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            result = list_workspace_usage()

        assert result == []


# ---------------------------------------------------------------------------
# find_orphan_workspaces
# ---------------------------------------------------------------------------


class TestFindOrphanWorkspaces:
    def test_no_orphans_when_all_known(self, tmp_path):
        root = tmp_path / "workspaces"
        for sid in ("sess_a", "sess_b"):
            ws = root / sid
            ws.mkdir(parents=True)
            (ws / "f").write_bytes(b"x")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            orphans = find_orphan_workspaces({"sess_a", "sess_b"})

        assert orphans == []

    def test_detects_orphaned_workspace(self, tmp_path):
        root = tmp_path / "workspaces"
        for sid in ("sess_a", "sess_b", "sess_orphan"):
            ws = root / sid
            ws.mkdir(parents=True)
            (ws / "f").write_bytes(b"x")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            orphans = find_orphan_workspaces({"sess_a", "sess_b"})

        assert len(orphans) == 1
        assert orphans[0]["session_id"] == "sess_orphan"

    def test_all_orphans_when_sessions_empty(self, tmp_path):
        root = tmp_path / "workspaces"
        for sid in ("sess_x", "sess_y"):
            (root / sid).mkdir(parents=True)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            orphans = find_orphan_workspaces(set())

        assert len(orphans) == 2


# ---------------------------------------------------------------------------
# cleanup_orphan_workspace
# ---------------------------------------------------------------------------


class TestCleanupOrphanWorkspace:
    def test_removes_orphan_directory(self, tmp_path):
        root = tmp_path / "workspaces"
        ws = root / "sess_gone"
        ws.mkdir(parents=True)
        (ws / "f.txt").write_bytes(b"data")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            cleanup_orphan_workspace(str(ws))

        assert not ws.exists()

    def test_raises_for_path_outside_root(self, tmp_path):
        from tether.workspace import WorkspaceError

        root = tmp_path / "workspaces"
        root.mkdir()
        outside = tmp_path / "outside" / "dir"
        outside.mkdir(parents=True)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)):
            with pytest.raises(WorkspaceError, match="outside"):
                cleanup_orphan_workspace(str(outside))


# ---------------------------------------------------------------------------
# API endpoint: GET /api/status/workspaces
# ---------------------------------------------------------------------------


class TestWorkspacesApiEndpoint:
    @pytest.mark.anyio
    async def test_returns_workspace_list(self, api_client, tmp_path):
        root = tmp_path / "workspaces"
        ws = root / "sess_abc123"
        ws.mkdir(parents=True)
        (ws / "code.py").write_bytes(b"x" * 2048)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = []
            resp = await api_client.get("/api/status/workspaces")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["workspaces"]) == 1
        ws_entry = data["workspaces"][0]
        assert ws_entry["session_id"] == "sess_abc123"
        assert ws_entry["size_bytes"] == 2048
        assert ws_entry["is_orphan"] is True
        assert data["orphan_count"] == 1

    @pytest.mark.anyio
    async def test_annotates_known_session(self, api_client, tmp_path):
        from tether.models import Session, SessionState

        root = tmp_path / "workspaces"
        ws = root / "sess_known"
        ws.mkdir(parents=True)
        (ws / "f").write_bytes(b"y" * 100)

        mock_session = Session(
            id="sess_known",
            repo_id="repo",
            state=SessionState.AWAITING_INPUT,
            last_activity_at="2026-01-01T00:00:00Z",
        )

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = [mock_session]
            resp = await api_client.get("/api/status/workspaces")

        assert resp.status_code == 200
        ws_entry = resp.json()["workspaces"][0]
        assert ws_entry["is_orphan"] is False
        assert ws_entry["session_state"] == "AWAITING_INPUT"

    @pytest.mark.anyio
    async def test_stale_only_excludes_active_sessions(self, api_client, tmp_path):
        from tether.models import Session, SessionState

        root = tmp_path / "workspaces"
        for sid, state in (
            ("sess_running", SessionState.RUNNING),
            ("sess_error", SessionState.ERROR),
        ):
            ws = root / sid
            ws.mkdir(parents=True)
            (ws / "f").write_bytes(b"z")

        sessions = [
            Session(id="sess_running", repo_id="r", state=SessionState.RUNNING),
            Session(id="sess_error", repo_id="r", state=SessionState.ERROR),
        ]

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = sessions
            resp = await api_client.get("/api/status/workspaces?stale_only=true")

        assert resp.status_code == 200
        ids = {w["session_id"] for w in resp.json()["workspaces"]}
        assert "sess_error" in ids
        assert "sess_running" not in ids

    @pytest.mark.anyio
    async def test_warning_when_disk_quota_exceeded(self, api_client, tmp_path):
        root = tmp_path / "workspaces"
        ws = root / "sess_big"
        ws.mkdir(parents=True)
        (ws / "f").write_bytes(b"z" * 100)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store, \
             patch("tether.settings.Settings.workspace_max_disk_gb", return_value=0.0):
            mock_store.list_sessions.return_value = []
            resp = await api_client.get("/api/status/workspaces")

        assert resp.status_code == 200
        assert resp.json()["warning"] is not None

    @pytest.mark.anyio
    async def test_no_warning_under_quota(self, api_client, tmp_path):
        root = tmp_path / "workspaces"
        ws = root / "sess_small"
        ws.mkdir(parents=True)
        (ws / "f").write_bytes(b"z" * 100)

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store, \
             patch("tether.settings.Settings.workspace_max_disk_gb", return_value=100.0):
            mock_store.list_sessions.return_value = []
            resp = await api_client.get("/api/status/workspaces")

        assert resp.status_code == 200
        assert resp.json()["warning"] is None


# ---------------------------------------------------------------------------
# API endpoint: DELETE /api/status/workspaces/orphans
# ---------------------------------------------------------------------------


class TestCleanupOrphansApiEndpoint:
    @pytest.mark.anyio
    async def test_removes_orphaned_workspaces(self, api_client, tmp_path):
        root = tmp_path / "workspaces"
        orphan_dir = root / "sess_orphan"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "f").write_bytes(b"trash")

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = []
            resp = await api_client.delete("/api/status/workspaces/orphans")

        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == 1
        assert data["errors"] == []
        assert not orphan_dir.exists()

    @pytest.mark.anyio
    async def test_no_orphans_returns_zero(self, api_client, tmp_path):
        root = tmp_path / "workspaces"
        root.mkdir()

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = []
            resp = await api_client.delete("/api/status/workspaces/orphans")

        assert resp.status_code == 200
        assert resp.json()["removed"] == 0

    @pytest.mark.anyio
    async def test_known_sessions_not_removed(self, api_client, tmp_path):
        from tether.models import Session, SessionState

        root = tmp_path / "workspaces"
        ws = root / "sess_active"
        ws.mkdir(parents=True)
        (ws / "f").write_bytes(b"keep me")

        sessions = [Session(id="sess_active", repo_id="r", state=SessionState.AWAITING_INPUT)]

        with patch("tether.workspace.managed_workspaces_dir", return_value=str(root)), \
             patch("tether.api.status.store") as mock_store:
            mock_store.list_sessions.return_value = sessions
            resp = await api_client.delete("/api/status/workspaces/orphans")

        assert resp.status_code == 200
        assert resp.json()["removed"] == 0
        assert ws.exists()


# ---------------------------------------------------------------------------
# Settings: workspace_max_disk_gb
# ---------------------------------------------------------------------------


class TestWorkspaceMaxDiskGb:
    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("TETHER_WORKSPACE_MAX_DISK_GB", raising=False)
        from tether.settings import settings
        assert settings.workspace_max_disk_gb() is None

    def test_returns_float_when_set(self, monkeypatch):
        monkeypatch.setenv("TETHER_WORKSPACE_MAX_DISK_GB", "50.5")
        from tether.settings import settings
        assert settings.workspace_max_disk_gb() == 50.5

    def test_returns_none_on_invalid_value(self, monkeypatch):
        monkeypatch.setenv("TETHER_WORKSPACE_MAX_DISK_GB", "not-a-number")
        from tether.settings import settings
        assert settings.workspace_max_disk_gb() is None


# ---------------------------------------------------------------------------
# Store: managed workdir cleanup on delete
# ---------------------------------------------------------------------------


class TestManagedWorkdirCleanup:
    """Verify that delete_session removes managed workspace directories."""

    def test_delete_removes_managed_workdir(self, tmp_path, fresh_store):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "file.txt").write_bytes(b"important data")

        session = fresh_store.create_session(repo_id="test", base_ref=None)
        fresh_store.set_workdir(session.id, str(ws), managed=True)

        fresh_store.delete_session(session.id)
        assert not ws.exists()

    def test_delete_does_not_remove_unmanaged_workdir(self, tmp_path, fresh_store):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "file.txt").write_bytes(b"keep me")

        session = fresh_store.create_session(repo_id="test", base_ref=None)
        fresh_store.set_workdir(session.id, str(ws), managed=False)

        fresh_store.delete_session(session.id)
        assert ws.exists()

    def test_prune_removes_managed_workdir(self, tmp_path, fresh_store):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "data").write_bytes(b"old")

        session = fresh_store.create_session(repo_id="old", base_ref=None)
        fresh_store.set_workdir(session.id, str(ws), managed=True)

        # Age the session: set last_activity_at to 2 days ago
        from tether.models import SessionState
        from datetime import datetime, timezone, timedelta

        session = fresh_store.get_session(session.id)
        old_dt = datetime.now(timezone.utc) - timedelta(days=2)
        old_ts = old_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        session.last_activity_at = old_ts
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)

        removed = fresh_store.prune_sessions(retention_days=1)
        assert removed == 1
        assert not ws.exists()
