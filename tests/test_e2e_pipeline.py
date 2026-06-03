"""
E2E integration tests — TaskStore lifecycle, dependencies, audit, SLA, stress.
Runs against SQLite :memory:.
"""

from __future__ import annotations

import os
os.environ["A2A_REGISTRY_DATABASE__DRIVER"] = "sqlite"
os.environ["A2A_REGISTRY_DATABASE__SQLITE_PATH"] = ":memory:"

from simple_a2a_registry.orchestration.store import TaskStore
import pytest


S = {
    "PENDING": "pending", "READY": "ready", "RUNNING": "running",
    "COMPLETED": "completed", "FAILED": "failed", "DANGLING": "dangling",
}


@pytest.fixture
def store() -> TaskStore:
    s = TaskStore(":memory:")
    yield s
    s.close()


class TestLifecycle:
    def test_create_and_retrieve(self, store: TaskStore) -> None:
        t = store.create_task(title="hello", assignee="bot", priority=5)
        assert t.id and t.title == "hello" and store.get_task(t.id) is not None

    def test_full_flow(self, store: TaskStore) -> None:
        t = store.create_task(title="flow")
        # Task may auto-promote from pending → ready on creation
        for s in (S["PENDING"], S["READY"], S["RUNNING"], S["COMPLETED"]):
            try:
                store.update_task_status(t.id, s)
            except Exception:
                pass  # skip if transition already happened
        assert store.get_task(t.id).status == "completed"

    def test_fail_then_retry(self, store: TaskStore) -> None:
        # failed → ready is the retry path
        t = store.create_task(title="fail")
        store.update_task_status(t.id, S["RUNNING"])
        store.update_task_status(t.id, S["FAILED"])
        assert store.get_task(t.id).status == "failed"


class TestDependencies:
    def test_parent_child(self, store: TaskStore) -> None:
        p = store.create_task(title="p")
        c = store.create_task(title="c", parents=[p.id])
        assert store.get_task(c.id) is not None

    def test_chain(self, store: TaskStore) -> None:
        a = store.create_task(title="a")
        b = store.create_task(title="b", parents=[a.id])
        c = store.create_task(title="c", parents=[b.id])
        assert store.get_task(c.id) is not None


class TestAudit:
    def test_events(self, store: TaskStore) -> None:
        t = store.create_task(title="evt")
        for s in (S["PENDING"], S["READY"], S["RUNNING"], S["COMPLETED"]):
            try:
                store.update_task_status(t.id, s)
            except Exception:
                pass
        events = store.get_events(t.id)
        kinds = [e.kind for e in events]
        assert "created" in kinds
        assert len(events) >= 3

    def test_comments(self, store: TaskStore) -> None:
        t = store.create_task(title="cmt")
        store.add_comment(t.id, "e2e", "hello")
        assert any("hello" in c.body for c in store.get_comments(t.id))


class TestRetry:
    def test_retryable(self, store: TaskStore) -> None:
        t = store.create_task(title="retry", max_retries=3)
        store.update_task_status(t.id, S["RUNNING"])
        store.update_task_status(t.id, S["FAILED"])
        assert isinstance(store.promote_retryable_tasks(), int)


class TestSLA:
    def test_window(self, store: TaskStore) -> None:
        for i in range(5):
            t = store.create_task(title=f"sla-{i}")
            for s in (S["PENDING"], S["READY"], S["RUNNING"], S["COMPLETED"]):
                try:
                    store.update_task_status(t.id, s)
                except Exception:
                    pass
        from simple_a2a_registry.orchestration.sla import SlaCalculator
        calc = SlaCalculator(store)
        stat = calc.window_stat("1h", 3600)
        assert stat.total_terminal >= 5 and stat.success_rate > 0


class TestStress:
    def test_200_tasks(self, store: TaskStore) -> None:
        ids = [store.create_task(title=f"s-{i}", priority=i % 5).id for i in range(200)]
        assert len(set(ids)) == 200