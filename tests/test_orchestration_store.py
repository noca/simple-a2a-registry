"""Tests for the orchestration SQLite store — CRUD, claim, dependencies, TTL."""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Generator

import pytest

from simple_a2a_registry.orchestration.models import (
    TaskStatus,
    TaskEventKind,
)
from simple_a2a_registry.orchestration.store import (
    TaskStore,
    DEFAULT_CLAIM_TTL,
    DEFAULT_MAX_RETRIES,
)
from simple_a2a_registry.orchestration.state_machine import InvalidTransitionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_create_simple_task(self, store: TaskStore) -> None:
        task = store.create_task(title="Test task", assignee="coder")
        assert task.id.startswith("t_")
        assert task.title == "Test task"
        assert task.assignee == "coder"
        assert task.status == TaskStatus.READY.value  # no parents → ready
        assert task.priority == 0
        assert task.created_at > 0

    def test_create_task_with_parents(self, store: TaskStore) -> None:
        parent = store.create_task(title="Parent")
        child = store.create_task(title="Child", parents=[parent.id])
        assert child.status == TaskStatus.TODO.value  # parent not done yet
        # Verify the link
        parents = store.get_parents(child.id)
        assert len(parents) == 1
        assert parents[0]["id"] == parent.id

    def test_create_task_promoted_when_parent_completed(self, store: TaskStore) -> None:
        parent = store.create_task(title="Parent")
        child = store.create_task(title="Child", parents=[parent.id])
        assert child.status == TaskStatus.TODO.value

        # Claim and complete parent → child should auto-promote
        store.claim_task(parent.id, "worker-1", 1)
        store.update_task_status(parent.id, TaskStatus.COMPLETED.value)
        updated = store.get_task(child.id)
        assert updated is not None
        assert updated.status == TaskStatus.READY.value

    def test_create_task_parent_not_found(self, store: TaskStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.create_task(title="Orphan", parents=["t_nonexistent"])

    def test_create_task_with_grandparent_parents(self, store: TaskStore) -> None:
        """A task with parent + grandparent as direct parents is valid (not a cycle)."""
        a = store.create_task(title="A")
        b = store.create_task(title="B", parents=[a.id])
        # C gets both B (parent) and A (grandparent) — no cycle
        c = store.create_task(title="C", parents=[b.id, a.id])
        assert c.status == TaskStatus.TODO.value
        assert len(store.get_parents(c.id)) == 2

    def test_create_task_with_existing_parent(self, store: TaskStore) -> None:
        """Creating a child of an existing task works fine (no self-link)."""
        task = store.create_task(title="Parent")
        child = store.create_task(title="Child", parents=[task.id])
        assert child.status == TaskStatus.TODO.value
        assert len(store.get_parents(child.id)) == 1


class TestGetTask:
    def test_get_existing(self, store: TaskStore) -> None:
        created = store.create_task(title="Find me")
        fetched = store.get_task(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == "Find me"

    def test_get_nonexistent(self, store: TaskStore) -> None:
        assert store.get_task("t_nonexistent") is None

    def test_get_task_includes_parents_and_children(self, store: TaskStore) -> None:
        p1 = store.create_task(title="P1")
        p2 = store.create_task(title="P2")
        child = store.create_task(title="Child", parents=[p1.id, p2.id])

        fetched = store.get_task(child.id)
        assert fetched is not None
        assert len(fetched.parents) == 2
        parent_ids = {pp["id"] for pp in fetched.parents}
        assert p1.id in parent_ids
        assert p2.id in parent_ids

        # Children of p1
        p1_fetched = store.get_task(p1.id)
        assert p1_fetched is not None
        assert len(p1_fetched.children) == 1
        assert p1_fetched.children[0]["id"] == child.id


class TestListTasks:
    def test_list_empty(self, store: TaskStore) -> None:
        tasks, total = store.list_tasks()
        assert tasks == []
        assert total == 0

    def test_list_all(self, store: TaskStore) -> None:
        store.create_task(title="A")
        store.create_task(title="B")
        tasks, total = store.list_tasks()
        assert total == 2
        assert len(tasks) == 2

    def test_list_filter_by_status(self, store: TaskStore) -> None:
        t1 = store.create_task(title="Ready one")
        t2 = store.create_task(title="Todo one", parents=[t1.id])
        ready_tasks, total = store.list_tasks(status="ready")
        assert total == 1
        assert ready_tasks[0].id == t1.id

        todo_tasks, total = store.list_tasks(status="todo")
        assert total == 1
        assert todo_tasks[0].id == t2.id

    def test_list_filter_by_assignee(self, store: TaskStore) -> None:
        store.create_task(title="A", assignee="alice")
        store.create_task(title="B", assignee="bob")
        tasks, total = store.list_tasks(assignee="alice")
        assert total == 1
        assert tasks[0].assignee == "alice"

    def test_list_filter_by_tenant(self, store: TaskStore) -> None:
        store.create_task(title="A", tenant="tenant1")
        store.create_task(title="B", tenant="tenant2")
        tasks, total = store.list_tasks(tenant="tenant1")
        assert total == 1

    def test_list_filter_by_q(self, store: TaskStore) -> None:
        store.create_task(title="Login module", body="Implement login")
        store.create_task(title="Logout module", body="Implement logout")
        tasks, total = store.list_tasks(q="Login")
        assert total == 1

    def test_list_pagination(self, store: TaskStore) -> None:
        for i in range(10):
            store.create_task(title=f"Task {i}")
        tasks, total = store.list_tasks(limit=3, offset=0)
        assert len(tasks) == 3
        assert total == 10

        # offset 3
        tasks2, _ = store.list_tasks(limit=3, offset=3)
        assert len(tasks2) == 3
        assert tasks2[0].id != tasks[0].id

    def test_list_sort_by_priority(self, store: TaskStore) -> None:
        store.create_task(title="Low", priority=1)
        store.create_task(title="High", priority=10)
        tasks, _ = store.list_tasks(sort="-priority")
        assert tasks[0].priority == 10
        assert tasks[-1].priority == 1


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


class TestUpdateTaskStatus:
    def test_valid_transition(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        assert task.status == TaskStatus.READY.value

        updated = store.update_task_status(task.id, TaskStatus.RUNNING.value)
        assert updated.status == TaskStatus.RUNNING.value

        updated2 = store.update_task_status(task.id, TaskStatus.COMPLETED.value)
        assert updated2.status == TaskStatus.COMPLETED.value

    def test_invalid_transition(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        # ready → completed is illegal
        with pytest.raises(InvalidTransitionError):
            store.update_task_status(task.id, TaskStatus.COMPLETED.value)

    def test_completed_sets_completed_at(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.COMPLETED.value)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.completed_at is not None
        assert updated.completed_at > 0

    def test_failed_increments_failure_count(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.FAILED.value)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.consecutive_failures == 1

    def test_completed_resets_failure_count(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        # Fail twice (promote back to ready after each)
        for _ in range(2):
            store.update_task_status(task.id, TaskStatus.RUNNING.value)
            store.update_task_status(task.id, TaskStatus.FAILED.value)
            store.promote_retryable_tasks()  # failed → ready
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.consecutive_failures == 2

        # Complete should reset failure count
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.COMPLETED.value)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.consecutive_failures == 0

    def test_blocked_unblocked(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)
        assert store.get_task(task.id).status == TaskStatus.BLOCKED.value  # type: ignore[union-attr]

        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        assert store.get_task(task.id).status == TaskStatus.RUNNING.value  # type: ignore[union-attr]

    def test_archive(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.COMPLETED.value)

        store.update_task_status(task.id, TaskStatus.ARCHIVED.value)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.ARCHIVED.value

        # No transition out of archived
        with pytest.raises(InvalidTransitionError):
            store.update_task_status(task.id, TaskStatus.READY.value)


# ---------------------------------------------------------------------------
# Atomic Claim
# ---------------------------------------------------------------------------


class TestClaimTask:
    def test_claim_success(self, store: TaskStore) -> None:
        task = store.create_task(title="Claimable", assignee="coder")
        result = store.claim_task(task.id, "worker-1", 12345)
        assert result is not None
        assert result["task_id"] == task.id
        assert result["claim_lock"] == "worker-1:12345"
        assert result["claim_expires"] > 0

        # Task should be running
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.RUNNING.value
        assert updated.claim_lock == "worker-1:12345"
        assert updated.worker_pid == 12345
        assert updated.current_run_id is not None

    def test_claim_twice_fails(self, store: TaskStore) -> None:
        task = store.create_task(title="Claimable", assignee="coder")
        assert store.claim_task(task.id, "worker-1", 1) is not None
        # Second claim should fail
        assert store.claim_task(task.id, "worker-2", 2) is None

    def test_claim_not_ready_fails(self, store: TaskStore) -> None:
        task = store.create_task(title="Already running", assignee="coder")
        store.claim_task(task.id, "worker-1", 1)
        assert store.claim_task(task.id, "worker-2", 2) is None

    def test_claim_creates_run_record(self, store: TaskStore) -> None:
        task = store.create_task(title="Run test", assignee="coder")
        store.claim_task(task.id, "worker-1", 12345)
        runs = store.get_runs(task.id)
        assert len(runs) == 1
        assert runs[0].profile == "worker-1"
        assert runs[0].status == "running"
        assert runs[0].claim_lock == "worker-1:12345"

    def test_claim_expired_then_reclaim(self, store: TaskStore) -> None:
        task = store.create_task(title="Reclaimable", assignee="coder")
        store.claim_task(task.id, "worker-1", 1, ttl=1)
        time.sleep(1.5)
        # Release expired claim → task goes to failed
        released = store.release_expired_claims()
        assert released == 1
        # Promote back to ready
        promoted = store.promote_retryable_tasks()
        assert promoted == 1
        # Now should be able to reclaim
        result = store.claim_task(task.id, "worker-2", 2)
        assert result is not None
        assert result["claim_lock"] == "worker-2:2"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_extends_ttl(self, store: TaskStore) -> None:
        task = store.create_task(title="Heartbeat me", assignee="coder")
        store.claim_task(task.id, "worker-1", 1, ttl=10)
        old_task = store.get_task(task.id)
        assert old_task is not None
        old_expires = old_task.claim_expires

        time.sleep(0.1)
        assert store.heartbeat(task.id, "worker-1:1") is True
        new_task = store.get_task(task.id)
        assert new_task is not None
        new_expires = new_task.claim_expires
        assert new_expires is not None and old_expires is not None
        assert new_expires > old_expires

    def test_heartbeat_bad_lock(self, store: TaskStore) -> None:
        task = store.create_task(title="Locked", assignee="coder")
        store.claim_task(task.id, "worker-1", 1)
        assert store.heartbeat(task.id, "wrong:lock") is False

    def test_heartbeat_nonexistent(self, store: TaskStore) -> None:
        assert store.heartbeat("t_nonexistent", "anything") is False


# ---------------------------------------------------------------------------
# TTL Release
# ---------------------------------------------------------------------------


class TestTTLRelease:
    def test_release_expired_running(self, store: TaskStore) -> None:
        task = store.create_task(title="Expire me", assignee="coder")
        store.claim_task(task.id, "worker-1", 1, ttl=0)  # immediate expire
        time.sleep(0.1)
        released = store.release_expired_claims()
        assert released == 1

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.FAILED.value
        assert updated.last_failure_error == "Claim TTL expired"

    def test_release_expired_blocked(self, store: TaskStore) -> None:
        task = store.create_task(title="Block+expire", assignee="coder")
        store.claim_task(task.id, "worker-1", 1, ttl=0)
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)
        time.sleep(0.1)
        released = store.release_expired_claims()
        assert released == 1

    def test_no_release_for_active_tasks(self, store: TaskStore) -> None:
        task = store.create_task(title="Active", assignee="coder")
        store.claim_task(task.id, "worker-1", 1, ttl=600)
        released = store.release_expired_claims()
        assert released == 0
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.RUNNING.value


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_promote_retryable_task(self, store: TaskStore) -> None:
        task = store.create_task(title="Retry me", assignee="coder", max_retries=3)
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.FAILED.value)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.consecutive_failures == 1

        promoted = store.promote_retryable_tasks()
        assert promoted == 1

        updated2 = store.get_task(task.id)
        assert updated2 is not None
        assert updated2.status == TaskStatus.READY.value

    def test_exhaust_retries(self, store: TaskStore) -> None:
        task = store.create_task(title="Exhaust me", assignee="coder", max_retries=2)
        # Fail 3 times — promote after each allowed retry
        for i in range(3):
            store.update_task_status(task.id, TaskStatus.RUNNING.value)
            store.update_task_status(task.id, TaskStatus.FAILED.value)
            if i < 2:  # after first 2 failures, still retryable
                store.promote_retryable_tasks()

        # After 3rd failure, should be exhausted
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.consecutive_failures == 3
        assert updated.status == TaskStatus.FAILED.value

        # promote_retryable_tasks should not promote
        promoted = store.promote_retryable_tasks()
        assert promoted == 0


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class TestComments:
    def test_add_comment(self, store: TaskStore) -> None:
        task = store.create_task(title="Discuss")
        comment = store.add_comment(task.id, "alice", "Looks good!")
        assert comment.id > 0
        assert comment.author == "alice"
        assert comment.body == "Looks good!"

    def test_get_comments(self, store: TaskStore) -> None:
        task = store.create_task(title="Discuss")
        store.add_comment(task.id, "alice", "First")
        store.add_comment(task.id, "bob", "Second")
        comments = store.get_comments(task.id)
        assert len(comments) == 2
        assert comments[0].body == "First"
        assert comments[1].body == "Second"

    def test_add_comment_nonexistent_task(self, store: TaskStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.add_comment("t_nonexistent", "alice", "Hello")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_created_event(self, store: TaskStore) -> None:
        task = store.create_task(title="Event test")
        events = store.get_events(task.id)
        assert len(events) >= 1
        assert events[-1].kind == TaskEventKind.CREATED.value

    def test_status_change_events(self, store: TaskStore) -> None:
        task = store.create_task(title="Event chain")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.COMPLETED.value)
        events = store.get_events(task.id)
        kinds = [e.kind for e in events]
        assert "started" in kinds or TaskEventKind.CLAIMED.value in kinds
        assert TaskEventKind.COMPLETED.value in kinds

    def test_event_payload(self, store: TaskStore) -> None:
        task = store.create_task(title="Payload check")
        events = store.get_events(task.id)
        created_event = [e for e in events if e.kind == TaskEventKind.CREATED.value][0]
        assert created_event.payload is not None
        payload = json.loads(created_event.payload)
        assert "status" in payload

    def test_unblocked_audit_event(self, store: TaskStore) -> None:
        """Verify blocked → running emits UNBLOCKED event, not STARTED."""
        task = store.create_task(title="Unblock event test")
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)
        store.update_task_status(task.id, TaskStatus.RUNNING.value)
        events = store.get_events(task.id)
        kinds = [e.kind for e in events]
        assert TaskEventKind.UNBLOCKED.value in kinds,             f"Expected 'unblocked' in events, got: {kinds}"
        # Events are latest-first (ORDER BY id DESC), so unblocked comes first
        blocked_idx = kinds.index(TaskEventKind.BLOCKED.value)
        unblocked_idx = kinds.index(TaskEventKind.UNBLOCKED.value)
        assert unblocked_idx < blocked_idx

    def test_released_audit_event_on_ttl_expiry(self, store: TaskStore) -> None:
        """Verify TTL expiry emits a 'released' audit event."""
        task = store.create_task(title="TTL release event test")
        store.claim_task(task.id, "worker-1", 1, ttl=0)  # immediate expire
        time.sleep(0.1)
        released = store.release_expired_claims()
        assert released == 1
        events = store.get_events(task.id)
        kinds = [e.kind for e in events]
        assert "released" in kinds,             f"Expected 'released' in events, got: {kinds}"
        assert TaskEventKind.FAILED.value in kinds


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


class TestDependencies:
    def test_add_dependency(self, store: TaskStore) -> None:
        parent = store.create_task(title="Parent")
        child = store.create_task(title="Child")
        store.add_dependency(child.id, parent.id)
        parents = store.get_parents(child.id)
        assert len(parents) == 1
        assert parents[0]["id"] == parent.id

    def test_add_dependency_nonexistent(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        with pytest.raises(ValueError, match="not found"):
            store.add_dependency(task.id, "t_nonexistent")

    def test_add_dependency_self_link(self, store: TaskStore) -> None:
        task = store.create_task(title="Self")
        with pytest.raises(ValueError, match="Self|self-link|not allowed"):
            store.add_dependency(task.id, task.id)

    def test_add_dependency_cycle(self, store: TaskStore) -> None:
        a = store.create_task(title="A")
        b = store.create_task(title="B")
        store.add_dependency(b.id, a.id)
        # A → B is a cycle since B already depends on A
        with pytest.raises(ValueError, match="cycle"):
            store.add_dependency(a.id, b.id)

    def test_remove_dependency(self, store: TaskStore) -> None:
        parent = store.create_task(title="Parent")
        child = store.create_task(title="Child")
        store.add_dependency(child.id, parent.id)
        assert store.remove_dependency(child.id, parent.id) is True
        assert store.get_parents(child.id) == []

    def test_remove_nonexistent(self, store: TaskStore) -> None:
        task = store.create_task(title="Task")
        assert store.remove_dependency(task.id, "t_nonexistent") is False

    def test_dependency_promotion_on_complete(self, store: TaskStore) -> None:
        parent = store.create_task(title="Parent")
        child = store.create_task(title="Child", parents=[parent.id])
        assert child.status == TaskStatus.TODO.value

        store.update_task_status(parent.id, TaskStatus.RUNNING.value)
        store.update_task_status(parent.id, TaskStatus.COMPLETED.value)
        updated = store.get_task(child.id)
        assert updated is not None
        assert updated.status == TaskStatus.READY.value


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_store(self, store: TaskStore) -> None:
        stats = store.stats()
        assert stats["total"] == 0
        assert stats["by_status"] == {}

    def test_stats_by_status(self, store: TaskStore) -> None:
        store.create_task(title="A")  # ready
        t2 = store.create_task(title="B")
        store.update_task_status(t2.id, TaskStatus.RUNNING.value)  # running
        t3 = store.create_task(title="C", parents=[t2.id])  # todo

        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_status"].get("ready") == 1
        assert stats["by_status"].get("running") == 1
        assert stats["by_status"].get("todo") == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_create_task_no_assignee(self, store: TaskStore) -> None:
        task = store.create_task(title="Unassigned")
        assert task.assignee is None

    def test_claim_unassigned_task(self, store: TaskStore) -> None:
        task = store.create_task(title="Unassigned")
        result = store.claim_task(task.id, "worker-1", 1)
        assert result is not None

    def test_long_title_and_body(self, store: TaskStore) -> None:
        long_title = "A" * 500
        long_body = "B" * 5000
        task = store.create_task(title=long_title, body=long_body)
        assert task.title == long_title
        assert task.body == long_body

    def test_get_task_with_full_detail(self, store: TaskStore) -> None:
        """Verify that get_task returns runs, comments, and events when available."""
        task = store.create_task(title="Full detail")
        store.add_comment(task.id, "alice", "Comment 1")
        store.claim_task(task.id, "worker-1", 12345)

        fetched = store.get_task(task.id)
        assert fetched is not None
        # runs loaded via separate method
        runs = store.get_runs(task.id)
        assert len(runs) >= 1
        comments = store.get_comments(task.id)
        assert len(comments) >= 1
        events = store.get_events(task.id)
        assert len(events) >= 2  # created + claimed

    def test_event_limit(self, store: TaskStore) -> None:
        task = store.create_task(title="Event limit")
        for _ in range(5):
            store.add_comment(task.id, "bot", "spam")
        events = store.get_events(task.id, limit=2)
        assert len(events) <= 2


# ---------------------------------------------------------------------------
# Tenant-filtered operations
# ---------------------------------------------------------------------------


class TestTenantFilteredTTLRelease:
    """release_expired_claims(tenant=...) should only affect matching tasks."""

    def test_release_expired_claims_respects_tenant(self, store: TaskStore) -> None:
        """Only release tasks belonging to the specified tenant."""
        t_a = store.create_task(
            title="A-tenant", assignee="coder", tenant="tenant-a", max_retries=0,
        )
        t_b = store.create_task(
            title="B-tenant", assignee="coder", tenant="tenant-b", max_retries=0,
        )

        # Claim both with 0 TTL
        store.claim_task(t_a.id, "worker-1", 1, ttl=0)
        store.claim_task(t_b.id, "worker-1", 2, ttl=0)
        time.sleep(0.1)

        # Release only tenant-a's expired claims
        released = store.release_expired_claims(tenant="tenant-a")
        assert released == 1, f"Expected 1 release, got {released}"

        # tenant-a task should be failed, tenant-b should still be running
        a = store.get_task(t_a.id)
        assert a is not None
        assert a.status == TaskStatus.FAILED.value

        b = store.get_task(t_b.id)
        assert b is not None
        assert b.status == TaskStatus.RUNNING.value, (
            f"tenant-b task should still be running, got {b.status}"
        )

    def test_release_expired_claims_no_tenant_processes_all(
        self, store: TaskStore,
    ) -> None:
        """Without tenant filter, release all expired claims regardless of tenant."""
        t_a = store.create_task(
            title="A", assignee="coder", tenant="tenant-a", max_retries=0,
        )
        t_b = store.create_task(
            title="B", assignee="coder", tenant="tenant-b", max_retries=0,
        )

        store.claim_task(t_a.id, "worker-1", 1, ttl=0)
        store.claim_task(t_b.id, "worker-1", 2, ttl=0)
        time.sleep(0.1)

        released = store.release_expired_claims()  # no tenant filter
        assert released == 2, f"Expected 2 releases, got {released}"

    def test_release_expired_claims_wrong_tenant_noop(self, store: TaskStore) -> None:
        """Releasing expired claims for a tenant with none returns 0."""
        task = store.create_task(
            title="T", assignee="coder", tenant="tenant-a", max_retries=0,
        )
        store.claim_task(task.id, "worker-1", 1, ttl=600)  # long TTL

        released = store.release_expired_claims(tenant="tenant-b")
        assert released == 0


class TestTenantFilteredPromoteRetry:
    """promote_retryable_tasks(tenant=...) should only affect matching tasks."""

    def test_promote_respects_tenant(self, store: TaskStore) -> None:
        """Only promote failed tasks from the specified tenant."""
        t_a = store.create_task(
            title="A", assignee="coder", tenant="tenant-a", max_retries=3,
        )
        t_b = store.create_task(
            title="B", assignee="coder", tenant="tenant-b", max_retries=3,
        )

        # Fail both tasks
        for t in (t_a, t_b):
            store.update_task_status(t.id, TaskStatus.RUNNING.value)
            store.update_task_status(t.id, TaskStatus.FAILED.value)
            # Set consecutive_failures = 1 so retry logic picks it up
            with store._tx() as cur:
                cur.execute(
                    "UPDATE tasks SET consecutive_failures=1 WHERE id=?", (t.id,)
                )

        # Promote only tenant-a
        promoted = store.promote_retryable_tasks(tenant="tenant-a")
        assert promoted == 1, f"Expected 1 promotion, got {promoted}"

        a = store.get_task(t_a.id)
        assert a is not None
        assert a.status == TaskStatus.READY.value

        b = store.get_task(t_b.id)
        assert b is not None
        assert b.status == TaskStatus.FAILED.value

    def test_promote_no_tenant_processes_all(self, store: TaskStore) -> None:
        """Without tenant filter, promote all retryable tasks."""
        for tenant in ("tenant-a", "tenant-b"):
            t = store.create_task(
                title=f"T-{tenant}", assignee="coder", tenant=tenant, max_retries=3,
            )
            store.update_task_status(t.id, TaskStatus.RUNNING.value)
            store.update_task_status(t.id, TaskStatus.FAILED.value)
            with store._tx() as cur:
                cur.execute(
                    "UPDATE tasks SET consecutive_failures=1 WHERE id=?", (t.id,)
                )

        promoted = store.promote_retryable_tasks()  # no tenant filter
        assert promoted == 2


class TestTenantFilteredStats:
    """stats(tenant=...) should only count tasks from that tenant."""

    def test_stats_tenant_isolation(self, store: TaskStore) -> None:
        """stats(tenant=X) only counts tasks belonging to X."""
        for tenant in ("acme", "globex", None):
            store.create_task(
                title=f"Task-{tenant or 'none'}", tenant=tenant,
            )

        stats_acme = store.stats(tenant="acme")
        assert stats_acme["total"] == 1

        stats_all = store.stats()
        assert stats_all["total"] == 3  # includes None-tenanted tasks

    def test_stats_nonexistent_tenant(self, store: TaskStore) -> None:
        """stats(tenant=nonexistent) returns {total: 0, by_status: {}}."""
        store.create_task(title="T", tenant="tenant-a")
        stats = store.stats(tenant="no-such-tenant")
        assert stats["total"] == 0
        assert stats["by_status"] == {}

    def test_stats_no_tenant_returns_all(self, store: TaskStore) -> None:
        """stats() without tenant returns all tasks."""
        store.create_task(title="A", tenant="tenant-a")
        store.create_task(title="B", tenant=None)
        store.create_task(title="C")

        stats = store.stats()
        assert stats["total"] == 3
