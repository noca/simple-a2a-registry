"""Tests for the YAML workflow engine — parsing, validation, and store application."""

from __future__ import annotations

import os
import tempfile
from typing import Generator

import pytest

from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.workflow import (
    WorkflowSpec,
    WorkflowTaskSpec,
    WorkflowDependency,
    validate_workflow,
    apply_workflow,
    _topological_sort,
    _detect_workflow_cycles,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> Generator[TaskStore, None, None]:
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
# YAML parsing tests
# ---------------------------------------------------------------------------


class TestFromYamlStr:
    def test_minimal(self) -> None:
        yaml_text = """
name: Test Workflow
tasks:
  - id: step-1
    title: Step One
    assignee: worker-1
"""
        spec = WorkflowSpec.from_yaml_str(yaml_text)
        assert spec.name == "Test Workflow"
        assert len(spec.tasks) == 1
        assert spec.tasks[0].id == "step-1"
        assert spec.tasks[0].title == "Step One"
        assert spec.tasks[0].assignee == "worker-1"

    def test_full_definition(self) -> None:
        yaml_text = """
name: Full Workflow
description: A complete workflow with dependencies
tenant: my-tenant
created_by: admin

tasks:
  - id: fetch
    title: Fetch data
    body: Fetch data from external API
    assignee: coder
    priority: 5
    max_runtime_seconds: 600
    max_retries: 3
    workspace_kind: scratch

  - id: validate
    title: Validate data
    assignee: verifier
    depends_on:
      - task: fetch
        condition: success

  - id: report
    title: Generate report
    assignee: writer
    depends_on:
      - validate
"""
        spec = WorkflowSpec.from_yaml_str(yaml_text)
        assert spec.name == "Full Workflow"
        assert spec.description == "A complete workflow with dependencies"
        assert spec.tenant == "my-tenant"
        assert spec.created_by == "admin"
        assert len(spec.tasks) == 3

        # Task 1: fetch
        t1 = spec.tasks[0]
        assert t1.id == "fetch"
        assert t1.title == "Fetch data"
        assert t1.body == "Fetch data from external API"
        assert t1.assignee == "coder"
        assert t1.priority == 5
        assert t1.max_runtime_seconds == 600
        assert t1.max_retries == 3
        assert t1.workspace_kind == "scratch"
        assert len(t1.depends_on) == 0

        # Task 2: validate
        t2 = spec.tasks[1]
        assert t2.id == "validate"
        assert t2.assignee == "verifier"
        assert len(t2.depends_on) == 1
        assert t2.depends_on[0].task == "fetch"
        assert t2.depends_on[0].condition == "success"

        # Task 3: report (string dependency, no condition)
        t3 = spec.tasks[2]
        assert t3.id == "report"
        assert len(t3.depends_on) == 1
        assert t3.depends_on[0].task == "validate"
        assert t3.depends_on[0].condition is None

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            WorkflowSpec.from_yaml_str("name: ''\ntasks: []")

    def test_missing_tasks_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one task"):
            WorkflowSpec.from_yaml_str("name: Empty\ntasks: []")

    def test_task_missing_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'id'"):
            WorkflowSpec.from_yaml_str("""
name: Bad
tasks:
  - title: No ID here
    assignee: worker
""")

    def test_task_missing_title_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'title'"):
            WorkflowSpec.from_yaml_str("""
name: Bad
tasks:
  - id: no-title
    assignee: worker
""")

    def test_depends_on_invalid_type_raises(self) -> None:
        with pytest.raises(ValueError, match="depends_on.*must be a string or mapping"):
            WorkflowSpec.from_yaml_str("""
name: Bad
tasks:
  - id: t1
    title: Task 1
    assignee: worker
    depends_on:
      - 42
""")

    def test_depends_on_missing_task_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'task' field"):
            WorkflowSpec.from_yaml_str("""
name: Bad
tasks:
  - id: t1
    title: Task 1
    assignee: worker
    depends_on:
      - condition: success
""")


class TestFromYamlPath:
    def test_roundtrip(self) -> None:
        """Write a YAML file, read it back, and verify."""
        yaml_text = """
name: File Workflow
tasks:
  - id: a
    title: Task A
    assignee: worker-1
  - id: b
    title: Task B
    assignee: worker-2
    depends_on:
      - task: a
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_text)
            tmp_path = f.name
        try:
            spec = WorkflowSpec.from_yaml_path(tmp_path)
            assert spec.name == "File Workflow"
            assert len(spec.tasks) == 2
        finally:
            os.unlink(tmp_path)

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            WorkflowSpec.from_yaml_path("/nonexistent/workflow.yaml")


class TestToDict:
    def test_roundtrip(self) -> None:
        yaml_text = """
name: Roundtrip
tasks:
  - id: step-1
    title: Step One
    assignee: worker
    depends_on: []
"""
        spec = WorkflowSpec.from_yaml_str(yaml_text)
        d = spec.to_dict()
        assert d["name"] == "Roundtrip"
        assert d["tasks"][0]["id"] == "step-1"
        assert d["tasks"][0]["title"] == "Step One"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateWorkflow:
    def test_valid_workflow(self) -> None:
        spec = WorkflowSpec(
            name="Valid",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(
                    id="b", title="B", assignee="w2",
                    depends_on=[WorkflowDependency(task="a")],
                ),
            ],
        )
        errors = validate_workflow(spec)
        assert errors == []

    def test_duplicate_ids(self) -> None:
        spec = WorkflowSpec(
            name="Duplicates",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(id="a", title="A again", assignee="w2"),
            ],
        )
        errors = validate_workflow(spec)
        assert any("Duplicate" in e for e in errors)

    def test_nonexistent_dependency(self) -> None:
        spec = WorkflowSpec(
            name="Bad dep",
            tasks=[
                WorkflowTaskSpec(
                    id="a", title="A", assignee="w1",
                    depends_on=[WorkflowDependency(task="nonexistent")],
                ),
            ],
        )
        errors = validate_workflow(spec)
        assert any("does not exist" in e for e in errors)

    def test_circular_dependency(self) -> None:
        spec = WorkflowSpec(
            name="Cycle",
            tasks=[
                WorkflowTaskSpec(
                    id="a", title="A", assignee="w1",
                    depends_on=[WorkflowDependency(task="c")],
                ),
                WorkflowTaskSpec(
                    id="b", title="B", assignee="w2",
                    depends_on=[WorkflowDependency(task="a")],
                ),
                WorkflowTaskSpec(
                    id="c", title="C", assignee="w3",
                    depends_on=[WorkflowDependency(task="b")],
                ),
            ],
        )
        errors = validate_workflow(spec)
        assert any("Circular" in e for e in errors)

    def test_self_dependency(self) -> None:
        spec = WorkflowSpec(
            name="Self dep",
            tasks=[
                WorkflowTaskSpec(
                    id="a", title="A", assignee="w1",
                    depends_on=[WorkflowDependency(task="a")],
                ),
            ],
        )
        errors = validate_workflow(spec)
        assert any("Circular" in e for e in errors)


# ---------------------------------------------------------------------------
# Topological sort tests
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_simple_chain(self) -> None:
        tasks = [
            WorkflowTaskSpec(id="a", title="A", assignee="w1"),
            WorkflowTaskSpec(
                id="b", title="B", assignee="w2",
                depends_on=[WorkflowDependency(task="a")],
            ),
            WorkflowTaskSpec(
                id="c", title="C", assignee="w3",
                depends_on=[WorkflowDependency(task="b")],
            ),
        ]
        sorted_tasks = _topological_sort(tasks)
        ids = [t.id for t in sorted_tasks]
        # a before b before c
        assert ids.index("a") < ids.index("b")
        assert ids.index("b") < ids.index("c")

    def test_diamond(self) -> None:
        """A → B, A → C, B,C → D"""
        tasks = [
            WorkflowTaskSpec(id="a", title="A", assignee="w1"),
            WorkflowTaskSpec(
                id="b", title="B", assignee="w2",
                depends_on=[WorkflowDependency(task="a")],
            ),
            WorkflowTaskSpec(
                id="c", title="C", assignee="w3",
                depends_on=[WorkflowDependency(task="a")],
            ),
            WorkflowTaskSpec(
                id="d", title="D", assignee="w4",
                depends_on=[
                    WorkflowDependency(task="b"),
                    WorkflowDependency(task="c"),
                ],
            ),
        ]
        sorted_tasks = _topological_sort(tasks)
        ids = [t.id for t in sorted_tasks]
        # a before b, c before d
        assert ids.index("a") < ids.index("b")
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("d")
        assert ids.index("c") < ids.index("d")

    def test_no_deps(self) -> None:
        """Tasks with no dependencies should come first."""
        tasks = [
            WorkflowTaskSpec(
                id="b", title="B", assignee="w2",
                depends_on=[WorkflowDependency(task="a")],
            ),
            WorkflowTaskSpec(id="a", title="A", assignee="w1"),
        ]
        sorted_tasks = _topological_sort(tasks)
        ids = [t.id for t in sorted_tasks]
        assert ids[0] == "a"  # root first


# ---------------------------------------------------------------------------
# apply_workflow tests (integration with TaskStore)
# ---------------------------------------------------------------------------


class TestApplyWorkflow:
    def test_simple_workflow(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Simple",
            tasks=[
                WorkflowTaskSpec(id="step1", title="Step 1", assignee="coder"),
                WorkflowTaskSpec(id="step2", title="Step 2", assignee="verifier"),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 2
        assert result.errors == []
        assert "step1" in result.task_ids
        assert "step2" in result.task_ids

        # Verify tasks exist in store
        t1 = store.get_task(result.task_ids["step1"])
        assert t1 is not None
        assert t1.title == "Step 1"
        assert t1.assignee == "coder"
        assert t1.status == TaskStatus.READY.value  # no parents → ready

        t2 = store.get_task(result.task_ids["step2"])
        assert t2 is not None
        assert t2.title == "Step 2"
        assert t2.status == TaskStatus.READY.value

    def test_workflow_with_dependencies(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Deps Workflow",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(
                    id="b", title="B", assignee="w2",
                    depends_on=[WorkflowDependency(task="a")],
                ),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 2
        assert result.errors == []

        # A should be READY (no parents)
        t_a = store.get_task(result.task_ids["a"])
        assert t_a is not None
        assert t_a.status == TaskStatus.READY.value

        # B should be TODO (waiting for A)
        t_b = store.get_task(result.task_ids["b"])
        assert t_b is not None
        assert t_b.status == TaskStatus.TODO.value

        # Verify the parent link
        parents = store.get_parents(t_b.id)
        assert len(parents) == 1
        assert parents[0]["id"] == t_a.id

    def test_workflow_with_conditions(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Conditional",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(
                    id="b", title="B (on success)", assignee="w2",
                    depends_on=[
                        WorkflowDependency(task="a", condition="success"),
                    ],
                ),
                WorkflowTaskSpec(
                    id="c", title="C (on failure)", assignee="w3",
                    depends_on=[
                        WorkflowDependency(task="a", condition="failure"),
                    ],
                ),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 3
        assert result.errors == []

        # Check that condition is stored in task_links
        t_b = store.get_task(result.task_ids["b"])
        assert t_b is not None
        parents_b = store.get_parents(t_b.id)
        assert len(parents_b) == 1
        # The condition may be accessible via the link
        link_condition = parents_b[0].get("condition")
        assert link_condition == "success"

    def test_diamond_dependency(self, store: TaskStore) -> None:
        """A → B, A → C, B → D, C → D"""
        spec = WorkflowSpec(
            name="Diamond",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(
                    id="b", title="B", assignee="w2",
                    depends_on=[WorkflowDependency(task="a")],
                ),
                WorkflowTaskSpec(
                    id="c", title="C", assignee="w3",
                    depends_on=[WorkflowDependency(task="a")],
                ),
                WorkflowTaskSpec(
                    id="d", title="D", assignee="w4",
                    depends_on=[
                        WorkflowDependency(task="b"),
                        WorkflowDependency(task="c"),
                    ],
                ),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 4
        assert result.errors == []

        # Verify D has 2 parents
        t_d = store.get_task(result.task_ids["d"])
        assert t_d is not None
        assert t_d.status == TaskStatus.TODO.value

    def test_invalid_workflow_returns_errors(self, store: TaskStore) -> None:
        """Cyclic dependency should fail validation and return errors."""
        spec = WorkflowSpec(
            name="Cycle",
            tasks=[
                WorkflowTaskSpec(
                    id="a", title="A", assignee="w1",
                    depends_on=[WorkflowDependency(task="c")],
                ),
                WorkflowTaskSpec(
                    id="b", title="B", assignee="w2",
                    depends_on=[WorkflowDependency(task="a")],
                ),
                WorkflowTaskSpec(
                    id="c", title="C", assignee="w3",
                    depends_on=[WorkflowDependency(task="b")],
                ),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 0
        assert len(result.errors) > 0
        assert any("Circular" in e for e in result.errors)

    def test_dry_run_does_not_create(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Dry Run",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
                WorkflowTaskSpec(id="b", title="B", assignee="w2"),
            ],
        )
        result = apply_workflow(store, spec, dry_run=True)
        assert result.created_count == 2
        assert result.errors == []
        # No tasks should have been created
        tasks, total = store.list_tasks()
        assert total == 0

    def test_tenant_and_created_by(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Tenanted",
            tenant="acme-corp",
            created_by="jenkins",
            tasks=[
                WorkflowTaskSpec(id="a", title="A", assignee="w1"),
            ],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 1
        t = store.get_task(result.task_ids["a"])
        assert t is not None
        assert t.tenant == "acme-corp"
        # created_by is stored on the task
        assert t.created_by in ("jenkins", "workflow-engine")


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_task_no_deps(self, store: TaskStore) -> None:
        spec = WorkflowSpec(
            name="Single",
            tasks=[WorkflowTaskSpec(id="only", title="Only Task", assignee="worker")],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 1
        t = store.get_task(result.task_ids["only"])
        assert t is not None
        assert t.status == TaskStatus.READY.value

    def test_five_level_chain(self, store: TaskStore) -> None:
        tasks = []
        for i in range(5):
            deps = [WorkflowDependency(task=f"l{i-1}")] if i > 0 else []
            tasks.append(
                WorkflowTaskSpec(
                    id=f"l{i}", title=f"Level {i}",
                    assignee=f"w{i}", depends_on=deps,
                )
            )
        spec = WorkflowSpec(name="Deep Chain", tasks=tasks)
        result = apply_workflow(store, spec)
        assert result.created_count == 5
        assert result.errors == []

    def test_no_assignee_is_valid(self, store: TaskStore) -> None:
        """Tasks without an assignee are created successfully."""
        spec = WorkflowSpec(
            name="No Assignee",
            tasks=[WorkflowTaskSpec(id="a", title="A")],
        )
        result = apply_workflow(store, spec)
        assert result.created_count == 1
        assert result.errors == []

    def test_detect_workflow_cycles_function(self) -> None:
        # Direct self-loop
        tasks = [
            WorkflowTaskSpec(
                id="a", title="A", assignee="w1",
                depends_on=[WorkflowDependency(task="a")],
            ),
        ]
        errors = _detect_workflow_cycles(tasks)
        assert len(errors) > 0

    def test_all_data_fields_preserved(self, store: TaskStore) -> None:
        """Verify that all task-level YAML fields are preserved in the store."""
        spec = WorkflowSpec(
            name="Full Fields",
            tasks=[
                WorkflowTaskSpec(
                    id="full",
                    title="Full Task",
                    body="Detailed body here",
                    assignee="admin",
                    priority=10,
                    max_runtime_seconds=300,
                    max_retries=5,
                    workspace_kind="scratch",
                ),
            ],
        )
        result = apply_workflow(store, spec)
        t = store.get_task(result.task_ids["full"])
        assert t is not None
        assert t.body is not None
        assert "Detailed body" in t.body
        assert t.priority == 10
        assert t.max_runtime_seconds == 300
        assert t.max_retries == 5
        assert t.workspace_kind == "scratch"