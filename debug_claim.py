"""Debug script matching test setup exactly."""
import os
import tempfile
from pathlib import Path
from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore, DEFAULT_CLAIM_TTL
from simple_a2a_registry.orchestration.workspace import WorkspaceManager
from simple_a2a_registry.orchestration.dispatcher import Dispatcher, DispatcherConfig
import asyncio

async def main():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = TaskStore(db_path)
    print(f"DB path: {db_path}")

    task = store.create_task(title="claim-me", assignee="worker-1")
    print(f"Created task: id={task.id}, status={task.status}, assignee={task.assignee}")
    assert task.status == TaskStatus.READY.value

    config = DispatcherConfig(
        poll_interval=3600,
        claim_ttl=DEFAULT_CLAIM_TTL,
        failure_limit=3,
        dispatcher_id="test-dispatcher",
        worker_command="echo",  # EXACTLY like the test
    )

    with tempfile.TemporaryDirectory() as d:
        ws_mgr = WorkspaceManager(str(Path(d) / "workspaces"))
        dispatcher = Dispatcher(store, ws_mgr, config)

        print(f"dispatcher.ws_connections = {dispatcher.ws_connections}")
        print(f"dispatcher.config.worker_command = {dispatcher.config.worker_command!r}")

        stats = await dispatcher.trigger_poll_cycle()
        print(f"Poll stats: {stats}")

        # Check task status after poll
        refreshed = store.get_task(task.id)
        print(f"Task status after poll: {refreshed.status if refreshed else 'None'}")

    store.close()
    os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(main())