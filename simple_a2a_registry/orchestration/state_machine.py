"""State machine — validate and enumerate task lifecycle transitions.

Transitions are defined by the architecture doc section 5.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Set, Tuple

from simple_a2a_registry.orchestration.models import TaskStatus


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
# 8-state model (7 active + 1 terminal) as documented in architecture-v2.md §5
#
# Edges not listed are implicitly forbidden and raise ``InvalidTransitionError``.

_TRANSITIONS: Dict[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.TODO: {
        TaskStatus.READY,      # all parents completed → auto-promote
        TaskStatus.CANCELLED,  # manual cancel
    },
    TaskStatus.READY: {
        TaskStatus.RUNNING,    # worker claim
        TaskStatus.TODO,       # a parent was re-activated → demote
        TaskStatus.BLOCKED,    # assignee not connected
        TaskStatus.CANCELLED,  # manual cancel
    },
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,  # worker reports success
        TaskStatus.FAILED,     # worker reports failure / TTL timeout / crash
        TaskStatus.BLOCKED,    # human-in-the-loop block
        TaskStatus.CANCELLED,  # manual cancel
    },
    TaskStatus.BLOCKED: {
        TaskStatus.RUNNING,    # unblocked → worker resumes
        TaskStatus.READY,      # human review → back to pool for reassignment
        TaskStatus.FAILED,     # TTL timeout while blocked
        TaskStatus.CANCELLED,  # manual cancel
    },
    TaskStatus.COMPLETED: {
        TaskStatus.ARCHIVED,   # cleanup / archival
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,      # auto-retry (consecutive_failures <= max_retries)
        TaskStatus.ARCHIVED,   # cleanup / archival
        TaskStatus.CANCELLED,  # manual cancel
    },
    TaskStatus.CANCELLED: {
        TaskStatus.ARCHIVED,   # cleanup / archival
    },
    TaskStatus.ARCHIVED: set(),  # terminal — no transitions out
}

# Build a lookup-friendly frozen set of (from, to) pairs
VALID_TRANSITIONS: FrozenSet[Tuple[str, str]] = frozenset(
    (from_st.value, to_st.value)
    for from_st, to_set in _TRANSITIONS.items()
    for to_st in to_set
)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class InvalidTransitionError(ValueError):
    """Raised when ``validate_transition`` is called with an invalid transition."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid transition: '{from_status}' → '{to_status}'"
        )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_transition(from_status: str, to_status: str) -> bool:
    """Validate that ``from_status`` → ``to_status`` is a legal transition.

    Args:
        from_status: Current task status string (e.g. ``'running'``).
        to_status:   Desired next status string (e.g. ``'completed'``).

    Returns:
        ``True`` if the transition is valid.

    Raises:
        InvalidTransitionError: If the transition is not allowed by the state
            machine.  The exception includes both status values for error handling.
    """
    if (from_status, to_status) not in VALID_TRANSITIONS:
        raise InvalidTransitionError(from_status, to_status)
    return True
