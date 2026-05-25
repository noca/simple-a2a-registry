"""Tests for the orchestration state machine — all valid/invalid transitions."""

from __future__ import annotations

import pytest

from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.state_machine import (
    InvalidTransitionError,
    VALID_TRANSITIONS,
    validate_transition,
)


class TestValidateTransition:
    """Verify every allowed and forbidden transition."""

    # ------------------------------------------------------------------
    # Valid transitions
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "from_status, to_status",
        [
            # todo → ?
            (TaskStatus.TODO, TaskStatus.READY),
            (TaskStatus.TODO, TaskStatus.CANCELLED),
            # ready → ?
            (TaskStatus.READY, TaskStatus.RUNNING),
            (TaskStatus.READY, TaskStatus.TODO),
            (TaskStatus.READY, TaskStatus.CANCELLED),
            # running → ?
            (TaskStatus.RUNNING, TaskStatus.COMPLETED),
            (TaskStatus.RUNNING, TaskStatus.FAILED),
            (TaskStatus.RUNNING, TaskStatus.BLOCKED),
            (TaskStatus.RUNNING, TaskStatus.CANCELLED),
            # blocked → ?
            (TaskStatus.BLOCKED, TaskStatus.RUNNING),
            (TaskStatus.BLOCKED, TaskStatus.FAILED),
            (TaskStatus.BLOCKED, TaskStatus.CANCELLED),
            # completed → ?
            (TaskStatus.COMPLETED, TaskStatus.ARCHIVED),
            # failed → ?
            (TaskStatus.FAILED, TaskStatus.READY),
            (TaskStatus.FAILED, TaskStatus.ARCHIVED),
            (TaskStatus.FAILED, TaskStatus.CANCELLED),
            # cancelled → ?
            (TaskStatus.CANCELLED, TaskStatus.ARCHIVED),
        ],
    )
    def test_valid_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        """Every entry in the transition table should pass."""
        assert validate_transition(from_status.value, to_status.value) is True

    # ------------------------------------------------------------------
    # Invalid transitions (exhaustive)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "from_status, to_status",
        [
            # todo → invalid targets
            (TaskStatus.TODO, TaskStatus.TODO),
            (TaskStatus.TODO, TaskStatus.RUNNING),
            (TaskStatus.TODO, TaskStatus.BLOCKED),
            (TaskStatus.TODO, TaskStatus.COMPLETED),
            (TaskStatus.TODO, TaskStatus.FAILED),
            (TaskStatus.TODO, TaskStatus.ARCHIVED),
            # ready → invalid targets
            (TaskStatus.READY, TaskStatus.READY),
            (TaskStatus.READY, TaskStatus.BLOCKED),
            (TaskStatus.READY, TaskStatus.COMPLETED),
            (TaskStatus.READY, TaskStatus.FAILED),
            (TaskStatus.READY, TaskStatus.ARCHIVED),
            # running → invalid targets
            (TaskStatus.RUNNING, TaskStatus.TODO),
            (TaskStatus.RUNNING, TaskStatus.READY),
            (TaskStatus.RUNNING, TaskStatus.RUNNING),
            (TaskStatus.RUNNING, TaskStatus.ARCHIVED),
            # blocked → invalid targets
            (TaskStatus.BLOCKED, TaskStatus.TODO),
            (TaskStatus.BLOCKED, TaskStatus.READY),
            (TaskStatus.BLOCKED, TaskStatus.BLOCKED),
            (TaskStatus.BLOCKED, TaskStatus.COMPLETED),
            (TaskStatus.BLOCKED, TaskStatus.ARCHIVED),
            # completed → invalid targets
            (TaskStatus.COMPLETED, TaskStatus.TODO),
            (TaskStatus.COMPLETED, TaskStatus.READY),
            (TaskStatus.COMPLETED, TaskStatus.RUNNING),
            (TaskStatus.COMPLETED, TaskStatus.BLOCKED),
            (TaskStatus.COMPLETED, TaskStatus.COMPLETED),
            (TaskStatus.COMPLETED, TaskStatus.FAILED),
            (TaskStatus.COMPLETED, TaskStatus.CANCELLED),
            # failed → invalid targets
            (TaskStatus.FAILED, TaskStatus.TODO),
            (TaskStatus.FAILED, TaskStatus.RUNNING),
            (TaskStatus.FAILED, TaskStatus.BLOCKED),
            (TaskStatus.FAILED, TaskStatus.COMPLETED),
            (TaskStatus.FAILED, TaskStatus.FAILED),
            # cancelled → invalid targets
            (TaskStatus.CANCELLED, TaskStatus.TODO),
            (TaskStatus.CANCELLED, TaskStatus.READY),
            (TaskStatus.CANCELLED, TaskStatus.RUNNING),
            (TaskStatus.CANCELLED, TaskStatus.BLOCKED),
            (TaskStatus.CANCELLED, TaskStatus.COMPLETED),
            (TaskStatus.CANCELLED, TaskStatus.FAILED),
            (TaskStatus.CANCELLED, TaskStatus.CANCELLED),
            # archived → everything is invalid (terminal state)
            (TaskStatus.ARCHIVED, TaskStatus.TODO),
            (TaskStatus.ARCHIVED, TaskStatus.READY),
            (TaskStatus.ARCHIVED, TaskStatus.RUNNING),
            (TaskStatus.ARCHIVED, TaskStatus.BLOCKED),
            (TaskStatus.ARCHIVED, TaskStatus.COMPLETED),
            (TaskStatus.ARCHIVED, TaskStatus.FAILED),
            (TaskStatus.ARCHIVED, TaskStatus.CANCELLED),
            (TaskStatus.ARCHIVED, TaskStatus.ARCHIVED),
        ],
    )
    def test_invalid_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        """Every entry NOT in the transition table should raise."""
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition(from_status.value, to_status.value)
        assert exc_info.value.from_status == from_status.value
        assert exc_info.value.to_status == to_status.value
        assert "Invalid transition" in str(exc_info.value)

    # ------------------------------------------------------------------
    # VALID_TRANSITIONS completeness
    # ------------------------------------------------------------------

    def test_valid_transitions_contains_all_expected_pairs(self) -> None:
        """The frozen set should match our explicit list."""
        expected = {
            ("todo", "ready"),
            ("todo", "cancelled"),
            ("ready", "running"),
            ("ready", "todo"),
            ("ready", "cancelled"),
            ("running", "completed"),
            ("running", "failed"),
            ("running", "blocked"),
            ("running", "cancelled"),
            ("blocked", "running"),
            ("blocked", "failed"),
            ("blocked", "cancelled"),
            ("completed", "archived"),
            ("failed", "ready"),
            ("failed", "archived"),
            ("failed", "cancelled"),
            ("cancelled", "archived"),
        }
        assert VALID_TRANSITIONS == expected

    def test_invalid_transition_error_message(self) -> None:
        """The error message should include both status values."""
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition("running", "todo")
        msg = str(exc_info.value)
        assert "running" in msg
        assert "todo" in msg
