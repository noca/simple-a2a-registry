"""CLI subcommand for workflow operations — apply, validate, show.

Usage (via the ``a2a-registry`` entry point)::

    a2a-registry workflow apply <file.yaml> [--dry-run]
    a2a-registry workflow validate <file.yaml>
    a2a-registry workflow show <task_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.workflow import (
    WorkflowSpec,
    apply_workflow,
    validate_workflow,
)

logger = logging.getLogger("a2a_registry.cli_workflow")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(cfg_path: Optional[str] = None) -> TaskStore:
    """Create a TaskStore from config, falling back to default db path."""
    from simple_a2a_registry.config import load_config

    config = load_config(cfg_path)
    if isinstance(config, dict):
        db_path = config.get("store", {}).get("db_path", "~/.simple-a2a-registry/board.db")
    else:
        db_path = getattr(
            config.orchestration, "board_path",
            "~/.simple-a2a-registry/board.db",
        )
    return TaskStore(db_path)


# ---------------------------------------------------------------------------
# Sub-parser builder
# ---------------------------------------------------------------------------


def build_workflow_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``workflow`` subcommand group to the CLI parser."""
    sp = subparsers.add_parser(
        "workflow",
        help="Define and apply declarative YAML workflows",
        description="Manage declarative workflow definitions in YAML format.",
    )

    sp_sub = sp.add_subparsers(
        dest="workflow_command",
        title="workflow subcommands",
    )

    # --- apply ---
    apply_p = sp_sub.add_parser(
        "apply",
        help="Apply a YAML workflow definition (create tasks)",
        description=(
            "Parse a YAML workflow definition and create all tasks in the store."
        ),
    )
    apply_p.add_argument(
        "file",
        help="Path to the YAML workflow definition file",
    )
    apply_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show what would be created without making changes",
    )
    apply_p.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON (useful for scripting)",
    )
    apply_p.set_defaults(func=lambda a: handle_workflow_apply(
        a.file,
        dry_run=bool(getattr(a, "dry_run", False)),
        json_output=bool(getattr(a, "json", False)),
    ))

    # --- validate ---
    validate_p = sp_sub.add_parser(
        "validate",
        help="Validate a YAML workflow definition (no changes made)",
        description=(
            "Check a YAML workflow file for correctness without creating tasks."
        ),
    )
    validate_p.add_argument(
        "file",
        help="Path to the YAML workflow definition file",
    )
    validate_p.add_argument(
        "--json",
        action="store_true",
        help="Output validation result as JSON",
    )
    validate_p.set_defaults(func=lambda a: handle_workflow_validate(
        a.file,
        json_output=bool(getattr(a, "json", False)),
    ))

    # --- show ---
    show_p = sp_sub.add_parser(
        "show",
        help="Show details of a workflow task (experimental)",
        description="Look up a task that was created via a workflow definition.",
    )
    show_p.add_argument(
        "task_id",
        help="Persistent task id (e.g., t_xxxxxxxx)",
    )
    show_p.set_defaults(func=lambda a: handle_workflow_show(a.task_id))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def handle_workflow_apply(
    file_path: str, *, dry_run: bool = False, json_output: bool = False,
) -> int:
    """Parse a YAML workflow file and create all tasks."""
    try:
        spec = WorkflowSpec.from_yaml_path(file_path)
        logger.info(
            "Parsed workflow '%s' with %d task(s)",
            spec.name, len(spec.tasks),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"Error parsing workflow file: {e}", file=sys.stderr)
        return 1

    store = _get_store()
    try:
        result = apply_workflow(store, spec, dry_run=dry_run)
        store.close()
    except Exception as e:
        store.close()
        print(f"Error applying workflow: {e}", file=sys.stderr)
        return 1

    if json_output:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"\nWorkflow: {result.name}")
        print(f"Tasks created: {result.created_count}")
        if result.errors:
            print(f"Errors ({len(result.errors)}):")
            for err in result.errors:
                print(f"  ✗ {err}")
        if result.task_ids:
            print("\nTask mapping (logical id → persistent id):")
            for logical_id, persistent_id in result.task_ids.items():
                print(f"  {logical_id} → {persistent_id}")

    return 0 if not result.errors else 1


def handle_workflow_validate(
    file_path: str, *, json_output: bool = False,
) -> int:
    """Validate a YAML workflow file without creating tasks."""
    try:
        spec = WorkflowSpec.from_yaml_path(file_path)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error parsing workflow file: {e}", file=sys.stderr)
        return 1

    errors = validate_workflow(spec)

    if json_output:
        print(json.dumps({
            "name": spec.name,
            "task_count": len(spec.tasks),
            "valid": len(errors) == 0,
            "errors": errors,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"\nWorkflow: {spec.name}")
        print(f"Tasks: {len(spec.tasks)}")
        if errors:
            print(f"Validation FAILED — {len(errors)} error(s):")
            for err in errors:
                print(f"  ✗ {err}")
        else:
            print("Validation PASSED ✓")
            print("\nTask list:")
            for t in spec.tasks:
                deps = ", ".join(
                    f"{d.task}" + (f" ({d.condition})" if d.condition else "")
                    for d in t.depends_on
                )
                deps_str = f"  ← {deps}" if deps else ""
                print(f"  {t.id}: {t.title} [{t.assignee}]{deps_str}")

    return 0 if not errors else 1


def handle_workflow_show(task_id: str) -> int:
    """Show task details for a workflow-created task."""
    store = _get_store()
    try:
        task = store.get_task(task_id)
        if task is None:
            print(f"Task '{task_id}' not found", file=sys.stderr)
            return 1

        print(f"\nTask: {task.id}")
        print(f"Title: {task.title}")
        print(f"Status: {task.status}")
        print(f"Assignee: {task.assignee}")
        print(f"Created at: {task.created_at}")
        if task.parents:
            print("\nParents:")
            for p in task.parents:
                print(f"  {p['id']}: {p['title']} [{p['status']}]")
        if task.children:
            print("\nChildren:")
            for c in task.children:
                print(f"  {c['id']}: {c['title']} [{c['status']}]")

        store.close()
    except Exception as e:
        store.close()
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0

