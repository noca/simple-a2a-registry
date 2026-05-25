"""Unit tests for the Workspace Manager (workspace.py).

Tests cover all three modes (scratch, dir, worktree) and edge cases
including validation, error handling, and cleanup.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from simple_a2a_registry.orchestration.models import Task
from simple_a2a_registry.orchestration.workspace import (
    WorkspaceManager,
    WorkspaceAllocationError,
    WorkspaceCleanupError,
    VALID_KINDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_root() -> Generator[Path, None, None]:
    """Create a temporary root for workspace directories."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def ws_mgr(tmp_root: Path) -> WorkspaceManager:
    """Create a WorkspaceManager rooted at the temporary directory."""
    return WorkspaceManager(str(tmp_root / "workspaces"))


def make_task(
    task_id: str = "t_test",
    kind: str | None = "scratch",
    workspace_path: str | None = None,
) -> Task:
    """Helper to create a minimal Task for testing."""
    return Task(
        id=task_id,
        title="test task",
        workspace_kind=kind,
        workspace_path=workspace_path,
    )


# ===================================================================
# Kind validation
# ===================================================================


class TestValidateKind:
    def test_scratch_is_valid(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("scratch") == "scratch"

    def test_dir_is_valid(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("dir") == "dir"

    def test_worktree_is_valid(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("worktree") == "worktree"

    def test_none_defaults_to_scratch(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind(None) == "scratch"

    def test_empty_defaults_to_scratch(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("") == "scratch"

    def test_case_insensitive(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("SCRATCH") == "scratch"
        assert ws_mgr.validate_kind("Dir") == "dir"

    def test_unknown_kind_raises(self, ws_mgr: WorkspaceManager) -> None:
        with pytest.raises(WorkspaceAllocationError, match="Unknown workspace kind"):
            ws_mgr.validate_kind("invalid")


# ===================================================================
# Scratch mode
# ===================================================================


class TestScratch:
    def test_allocate_creates_directory(self, ws_mgr: WorkspaceManager, tmp_root: Path) -> None:
        task = make_task("t_scratch_1")
        path = ws_mgr.allocate_for_claim(task)

        assert os.path.isdir(path)
        assert task.workspace_path == path
        # The path should be <workspaces_root>/<task_id>
        assert path.endswith("t_scratch_1")

    def test_allocate_idempotent(self, ws_mgr: WorkspaceManager) -> None:
        """Allocating the same task twice should not fail."""
        task = make_task("t_double")
        path1 = ws_mgr.allocate_for_claim(task)
        path2 = ws_mgr.allocate_for_claim(task)
        assert path1 == path2
        assert os.path.isdir(path1)

    def test_cleanup_removes_directory(self, ws_mgr: WorkspaceManager) -> None:
        task = make_task("t_cleanup")
        ws_mgr.allocate_for_claim(task)
        assert os.path.isdir(task.workspace_path)

        ws_mgr.cleanup(task)
        assert not os.path.exists(task.workspace_path)

    def test_cleanup_no_path(self, ws_mgr: WorkspaceManager) -> None:
        """Cleanup should be a no-op if no workspace_path set."""
        task = make_task("t_nopath")
        # Should not raise
        ws_mgr.cleanup(task)

    def test_cleanup_already_gone(self, ws_mgr: WorkspaceManager) -> None:
        """Cleanup should be a no-op if directory already removed."""
        task = make_task("t_gone")
        ws_mgr.allocate_for_claim(task)
        os.rmdir(task.workspace_path)  # manually remove
        # Should not raise
        ws_mgr.cleanup(task)


# ===================================================================
# Dir mode
# ===================================================================


class TestDir:
    def test_validate_existing_dir(self, ws_mgr: WorkspaceManager, tmp_root: Path) -> None:
        """Dir mode should validate that the path exists."""
        target = tmp_root / "shared"
        target.mkdir(parents=True, exist_ok=True)

        task = make_task("t_dir1", kind="dir", workspace_path=str(target))
        path = ws_mgr.allocate_for_claim(task)
        assert path == str(target.resolve())

    def test_validate_non_existent_raises(self, ws_mgr: WorkspaceManager) -> None:
        """Dir mode should raise if path doesn't exist."""
        task = make_task(
            "t_dir_missing", kind="dir",
            workspace_path="/tmp/does_not_exist_12345",
        )
        with pytest.raises(
            WorkspaceAllocationError, match="does not exist"
        ):
            ws_mgr.allocate_for_claim(task)

    def test_validate_no_path_raises(self, ws_mgr: WorkspaceManager) -> None:
        """Dir mode should raise if workspace_path is not set."""
        task = make_task("t_dir_nopath", kind="dir", workspace_path=None)
        with pytest.raises(
            WorkspaceAllocationError, match="no 'workspace_path' configured"
        ):
            ws_mgr.allocate_for_claim(task)

    def test_cleanup_noop(self, ws_mgr: WorkspaceManager, tmp_root: Path) -> None:
        """Dir mode cleanup should be a no-op."""
        target = tmp_root / "shared"
        target.mkdir(parents=True, exist_ok=True)

        task = make_task("t_dir_clean", kind="dir", workspace_path=str(target))
        ws_mgr.allocate_for_claim(task)
        ws_mgr.cleanup(task)
        # Directory should still exist (no auto-cleanup)
        assert target.exists()


# ===================================================================
# Worktree mode
# ===================================================================


class TestWorktree:
    def test_validate_kind(self, ws_mgr: WorkspaceManager) -> None:
        assert ws_mgr.validate_kind("worktree") == "worktree"

    def test_allocate_without_git_raises(self, ws_mgr: WorkspaceManager) -> None:
        """If no git repo is available, worktree allocation should raise."""
        task = make_task("t_wt1", kind="worktree")

        # Move to a non-git directory to ensure failure
        old_cwd = Path.cwd()
        try:
            os.chdir("/tmp")
            with pytest.raises(
                WorkspaceAllocationError,
                match="Git is not available|not available",
            ):
                ws_mgr.allocate_for_claim(task)
        finally:
            os.chdir(str(old_cwd))

    def test_cleanup_no_path(self, ws_mgr: WorkspaceManager) -> None:
        """Cleanup should be a no-op on a worktree task with no path."""
        task = make_task("t_wt_nopath", kind="worktree")
        ws_mgr.cleanup(task)  # should not raise


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    def test_unknown_kind_in_allocate(self, ws_mgr: WorkspaceManager) -> None:
        task = make_task("t_badkind", kind="magic")
        with pytest.raises(WorkspaceAllocationError):
            ws_mgr.allocate(task)

    def test_unknown_kind_in_cleanup(self, ws_mgr: WorkspaceManager) -> None:
        task = make_task("t_badclean", kind="magic", workspace_path="/tmp")
        with pytest.raises(WorkspaceAllocationError):
            ws_mgr.cleanup(task)

    def test_allocate_separate_directories(self, ws_mgr: WorkspaceManager) -> None:
        """Each task should get its own directory in scratch mode."""
        t1 = make_task("t_a")
        t2 = make_task("t_b")
        p1 = ws_mgr.allocate_for_claim(t1)
        p2 = ws_mgr.allocate_for_claim(t2)
        assert p1 != p2
        assert os.path.isdir(p1)
        assert os.path.isdir(p2)