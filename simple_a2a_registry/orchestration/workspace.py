"""Workspace Manager — allocate, validate, and clean up task workspaces.

Three modes as defined in architecture-v2.md §8:

- **scratch**: ephemeral sandbox directory created at claim time, deleted on archive
- **dir**:      shared persistent directory, must exist at claim time, never auto-cleaned
- **worktree**: Git worktree created via ``git worktree add`` at claim time,
               removed via ``git worktree remove`` on archive
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from simple_a2a_registry.orchestration.models import Task, TaskStatus

logger = logging.getLogger("a2a_registry.orchestration.workspace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_KINDS = frozenset({"scratch", "dir", "worktree"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkspaceError(RuntimeError):
    """Raised when a workspace operation fails."""


class WorkspaceAllocationError(WorkspaceError):
    """Raised when workspace creation or validation fails on claim."""


class WorkspaceCleanupError(WorkspaceError):
    """Raised when workspace cleanup fails on archive."""


# ---------------------------------------------------------------------------
# Workspace Manager
# ---------------------------------------------------------------------------


class WorkspaceManager:
    """Manage workspace lifecycle for tasks.

    Args:
        workspaces_root:
            Root directory for scratch workspaces.
            Defaults to ``<cwd>/workspaces``.
    """

    def __init__(
        self,
        workspaces_root: str = "workspaces",
    ) -> None:
        self._workspaces_root = Path(workspaces_root).expanduser().resolve()
        self._workspaces_root.mkdir(parents=True, exist_ok=True)
        logger.info(
            "WorkspaceManager initialised (root=%s)", self._workspaces_root
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_kind(self, kind: Optional[str]) -> str:
        """Normalise and validate a workspace kind string.

        Returns the validated kind, defaulting to ``'scratch'`` when *kind*
        is ``None`` or empty.

        Raises:
            WorkspaceAllocationError: If the kind is not recognised.
        """
        k = (kind or "scratch").strip().lower()
        if k not in VALID_KINDS:
            raise WorkspaceAllocationError(
                f"Unknown workspace kind '{kind}'; expected one of "
                f"{', '.join(sorted(VALID_KINDS))}"
            )
        return k

    def allocate(
        self,
        task: Task,
    ) -> str:
        """Allocate a workspace for *task* and return its absolute path.

        This is called during the claim flow.  For ``scratch`` tasks the
        directory is created; for ``dir`` tasks the pre-existing path is
        validated; for ``worktree`` tasks a git worktree is created.

        Raises:
            WorkspaceAllocationError: If the workspace cannot be prepared.
        """
        kind = self.validate_kind(task.workspace_kind)

        if kind == "scratch":
            return self._allocate_scratch(task)
        elif kind == "dir":
            return self._validate_dir(task)
        elif kind == "worktree":
            return self._allocate_worktree(task)
        else:
            raise WorkspaceAllocationError(f"Unhandled kind: {kind}")

    def cleanup(self, task: Task) -> None:
        """Clean up the workspace after a task is archived.

        For ``scratch`` tasks the directory is deleted.  For ``worktree``
        tasks the git worktree is removed.  ``dir`` is a no-op.

        Raises:
            WorkspaceCleanupError: If cleanup fails.
        """
        kind = self.validate_kind(task.workspace_kind)
        path = task.workspace_path
        if not path:
            return  # nothing to clean up

        if kind == "scratch":
            self._cleanup_scratch(task)
        elif kind == "worktree":
            self._cleanup_worktree(task)
        # dir: no-op — shared directory is not auto-cleaned

    def allocate_for_claim(
        self, task: Task,
    ) -> str:
        """Allocate workspace for a task being claimed.

        Convenience wrapper that validates, allocates, and returns the path.
        Also sets ``task.workspace_path`` as a side-effect.

        Returns:
            Absolute path to the allocated workspace.
        """
        path = self.allocate(task)
        task.workspace_path = path
        return path

    # ------------------------------------------------------------------
    # Scratch
    # ------------------------------------------------------------------

    def _allocate_scratch(self, task: Task) -> str:
        """Create a fresh scratch directory for the task."""
        ws_path = self._workspaces_root / task.id
        try:
            ws_path.mkdir(parents=True, exist_ok=True)
            logger.debug("Created scratch workspace %s", ws_path)
            return str(ws_path)
        except OSError as e:
            raise WorkspaceAllocationError(
                f"Failed to create scratch workspace for task '{task.id}': {e}"
            ) from e

    @staticmethod
    def _cleanup_scratch(task: Task) -> None:
        """Remove the scratch directory."""
        if not task.workspace_path:
            return
        path = Path(task.workspace_path)
        if not path.exists():
            logger.debug("Scratch workspace %s already gone", path)
            return
        try:
            shutil.rmtree(str(path))
            logger.debug("Removed scratch workspace %s", path)
        except OSError as e:
            raise WorkspaceCleanupError(
                f"Failed to remove scratch workspace '{path}' for "
                f"task '{task.id}': {e}"
            ) from e

    # ------------------------------------------------------------------
    # Dir
    # ------------------------------------------------------------------

    def _validate_dir(self, task: Task) -> str:
        """Validate that the pre-configured directory exists and is a directory.

        Raises:
            WorkspaceAllocationError: If the path is not set, does not exist,
                or is not a directory.
        """
        path_str = task.workspace_path
        if not path_str:
            raise WorkspaceAllocationError(
                f"Dir-mode task '{task.id}' has no 'workspace_path' configured"
            )
        path = Path(path_str).expanduser().resolve()
        if not path.exists():
            raise WorkspaceAllocationError(
                f"Dir-mode workspace '{path_str}' for task '{task.id}' "
                f"does not exist"
            )
        if not path.is_dir():
            raise WorkspaceAllocationError(
                f"Dir-mode workspace '{path_str}' for task '{task.id}' "
                f"is not a directory"
            )
        return str(path)

    # ------------------------------------------------------------------
    # Worktree
    # ------------------------------------------------------------------

    def _allocate_worktree(self, task: Task) -> str:
        """Create a git worktree for the task.

        The branch name is derived from the task id (``worktree/<task_id>``).
        The worktree path is ``<workspaces_root>/worktrees/<task_id>/``.

        Raises:
            WorkspaceAllocationError: If the worktree cannot be created.
        """
        branch = f"worktree/{task.id}"
        ws_path = self._workspaces_root / "worktrees" / task.id
        ws_path.parent.mkdir(parents=True, exist_ok=True)

        git_root = self._find_git_root(task)
        if not git_root:
            git_root = Path.cwd()

        if not self._git_available(git_root):
            raise WorkspaceAllocationError(
                f"Git is not available in '{git_root}'; cannot create worktree "
                f"for task '{task.id}'"
            )

        # Create branch if it doesn't exist
        _, rc = self._run_git_sync(
            git_root, ["rev-parse", "--verify", branch]
        )
        if rc != 0:
            _, rc2 = self._run_git_sync(
                git_root, ["checkout", "-b", branch]
            )
            if rc2 != 0:
                raise WorkspaceAllocationError(
                    f"Failed to create branch '{branch}' "
                    f"for task '{task.id}' (git may need a repo with at least one commit)"
                )

        # Add worktree
        out, rc = self._run_git_sync(
            git_root, ["worktree", "add", str(ws_path), branch]
        )
        if rc != 0:
            raise WorkspaceAllocationError(
                f"Failed to create worktree for task '{task.id}': {out}"
            )

        logger.debug("Created worktree at %s (branch=%s)", ws_path, branch)
        return str(ws_path)

    def _cleanup_worktree(self, task: Task) -> None:
        """Remove a git worktree."""
        if not task.workspace_path:
            return
        ws_path = Path(task.workspace_path)
        branch = f"worktree/{task.id}"

        git_root = self._find_git_root_from_path(ws_path) or ws_path.parent

        if not self._git_available(git_root):
            logger.warning(
                "Git not available; skipping worktree cleanup for task '%s'",
                task.id,
            )
            return

        # Remove worktree
        self._run_git_sync(git_root, ["worktree", "remove", str(ws_path)])
        logger.debug("Removed worktree %s", ws_path)

        # Best-effort branch deletion
        try:
            self._run_git_sync(git_root, ["branch", "-D", branch])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git_available(self, cwd: Path) -> bool:
        """Check if git is available in *cwd*."""
        try:
            out, rc = self._run_git_sync(
                cwd, ["rev-parse", "--is-inside-work-tree"], timeout=5
            )
            return rc == 0
        except Exception:
            return False

    @staticmethod
    def _find_git_root(task: Task) -> Optional[Path]:
        """Locate the git repository root for a task.

        Checks in order:
        1. ``workspace_path`` (if set)
        2. Current working directory
        """
        from pathlib import Path as _Path
        import subprocess

        candidates = []
        if task.workspace_path:
            candidates.append(_Path(task.workspace_path))
        candidates.append(_Path.cwd())

        for c in candidates:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    cwd=str(c),
                    timeout=10,
                )
                if result.returncode == 0:
                    return _Path(result.stdout.decode().strip())
            except Exception:
                continue
        return None

    @staticmethod
    def _find_git_root_from_path(path: Path) -> Optional[Path]:
        """Walk up from *path* to find a ``.git`` directory."""
        for parent in [path] + list(path.parents):
            if (parent / ".git").exists():
                return parent
        return None

    @staticmethod
    def _run_git_sync(
        cwd: Path,
        args: list[str],
        timeout: int = 30,
    ) -> tuple[str, int]:
        """Run a git command synchronously and return (output, returncode)."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                cwd=str(cwd),
                timeout=timeout,
            )
            output = (result.stderr or result.stdout or b"").decode(
                "utf-8", errors="replace"
            )
            return output, result.returncode
        except subprocess.TimeoutExpired:
            return f"Git command timed out after {timeout}s", -1
        except FileNotFoundError:
            return "Git not found", -1