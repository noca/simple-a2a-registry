"""CLI subcommands for task operations — view/search/filter tasks.

Usage (via the ``a2a-registry`` entry point)::

    a2a-registry task list [options]
    a2a-registry task show <task-id> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

DEFAULT_SERVER = "http://localhost:8321"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _api_get(path: str, params: Optional[Dict[str, Any]] = None,
             server: str = DEFAULT_SERVER) -> Dict[str, Any]:
    """Make a GET request to the Registry REST API."""
    if requests is None:
        print("Error: 'requests' library is required. Install with: pip install requests",
              file=sys.stderr)
        sys.exit(1)

    url = server.rstrip("/") + path
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.ConnectionError as e:
        print(f"Error: cannot connect to registry at {server}: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.Timeout:
        print(f"Error: request to {server} timed out", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Error: request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 200:
        return resp.json()
    else:
        try:
            body = resp.json()
            detail = body.get("detail", resp.text)
        except (json.JSONDecodeError, ValueError):
            detail = resp.text
        print(f"Error: {resp.status_code} — {detail}", file=sys.stderr)
        sys.exit(1)


def _api_get_raw(path: str, params: Optional[Dict[str, Any]] = None,
                 server: str = DEFAULT_SERVER) -> bytes:
    """Make a GET request and return raw bytes (for streaming / debugging)."""
    if requests is None:
        print("Error: 'requests' library is required.", file=sys.stderr)
        sys.exit(1)

    url = server.rstrip("/") + path
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"Error: request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except (json.JSONDecodeError, ValueError):
            detail = resp.text
        print(f"Error: {resp.status_code} — {detail}", file=sys.stderr)
        sys.exit(1)

    return resp.content


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_ts(ts: Optional[int]) -> str:
    """Format a unix timestamp to a readable string."""
    if ts is None:
        return "-"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(ts)


def _status_icon(status: str) -> str:
    """Return a small visual icon for the task status."""
    icons = {
        "todo": "○",
        "ready": "◉",
        "running": "▶",
        "dangling": "⚠",
        "blocked": "⊘",
        "completed": "✓",
        "failed": "✗",
        "cancelled": "✕",
        "archived": "◻",
    }
    return icons.get(status, "?")


def _print_task_table(tasks: List[Dict[str, Any]], total: int, limit: int,
                      offset: int, server: str) -> None:
    """Print tasks in a compact table format."""
    if not tasks:
        print("No tasks found.")
        return

    # Column widths
    id_width = max(len(t["id"]) for t in tasks) + 2
    status_width = 12
    assignee_width = max(len(t.get("assignee") or "-") for t in tasks) + 2
    title_width = 60

    # Header
    header = (
        f"{'ID':<{id_width}}"
        f"{'Status':<{status_width}}"
        f"{'Assignee':<{assignee_width}}"
        f"{'Title'}"
    )
    print(header)
    print("-" * len(header))

    for t in tasks:
        icon = _status_icon(t.get("status", ""))
        status_str = f"{icon} {t.get('status', 'unknown')}"
        assignee = t.get("assignee") or "-"
        title = (t.get("title") or "")[:title_width]
        print(
            f"{t['id']:<{id_width}}"
            f"{status_str:<{status_width}}"
            f"{assignee:<{assignee_width}}"
            f"{title}"
        )

    # Footer
    shown = len(tasks)
    print(f"\nShowing {shown} of {total} tasks (offset={offset}, limit={limit})")
    if total > offset + limit:
        next_offset = offset + limit
        print(f"Use --offset {next_offset} to see next page.")


def _print_task_detail(task: Dict[str, Any], parents: List[Dict[str, Any]],
                       children: List[Dict[str, Any]],
                       runs: List[Dict[str, Any]],
                       comments: List[Dict[str, Any]],
                       events: List[Dict[str, Any]]) -> None:
    """Print full task detail."""
    icon = _status_icon(task.get("status", ""))
    print(f"\n{'=' * 60}")
    print(f"  {icon}  {task.get('title', '(no title)')}")
    print(f"{'=' * 60}")

    # Basic info
    _kv("ID", task.get("id"))
    _kv("Status", task.get("status"))
    _kv("Assignee", task.get("assignee") or "-")
    _kv("Priority", str(task.get("priority", 0)))
    _kv("Tenant", str(task.get("tenant") or "-"))
    _kv("Created by", str(task.get("created_by") or "-"))
    _kv("Created at", _fmt_ts(task.get("created_at")))
    _kv("Started at", _fmt_ts(task.get("started_at")))
    _kv("Completed at", _fmt_ts(task.get("completed_at")))

    # Body
    body = task.get("body")
    if body:
        print(f"\n  Body:")
        for line in body.strip().split("\n"):
            print(f"    {line}")

    # Workspace
    if task.get("workspace_kind") or task.get("workspace_path"):
        print(f"\n  Workspace: {task.get('workspace_kind', '-')}")
        print(f"    Path: {task.get('workspace_path', '-')}")

    # Runtime
    if task.get("max_runtime_seconds"):
        _kv("Max runtime", f"{task['max_runtime_seconds']}s")
    if task.get("max_retries") is not None:
        _kv("Max retries", str(task["max_retries"]))
    if task.get("consecutive_failures"):
        _kv("Consecutive failures", str(task["consecutive_failures"]))
    if task.get("current_run_id"):
        _kv("Current run", f"#{task['current_run_id']}")

    # Runs
    if runs:
        print(f"\n  Runs ({len(runs)}):")
        for r in runs:
            o = r.get("outcome") or r.get("status", "running")
            pid = r.get("worker_pid") or "-"
            prof = r.get("profile") or "-"
            started = _fmt_ts(r.get("started_at"))
            ended = _fmt_ts(r.get("ended_at"))
            summary = r.get("summary") or ""
            s = f"    #{r.get('id', '?')} [{o}] profile={prof} pid={pid} {started}"
            if ended != "-":
                s += f" → {ended}"
            print(s)
            if summary:
                print(f"      Summary: {summary}")

    # Parents
    if parents:
        print(f"\n  Parents ({len(parents)}):")
        for p in parents:
            p_icon = _status_icon(p.get("status", ""))
            print(f"    {p_icon} {p['id']} — {p.get('title', '(no title)')} [{p.get('status', '?')}]")

    # Children
    if children:
        print(f"\n  Children ({len(children)}):")
        for c in children:
            c_icon = _status_icon(c.get("status", ""))
            print(f"    {c_icon} {c['id']} — {c.get('title', '(no title)')} [{c.get('status', '?')}]")

    # Comments
    if comments:
        print(f"\n  Comments ({len(comments)}):")
        for c in comments:
            author = c.get("author", "?")
            ts = _fmt_ts(c.get("created_at"))
            body = c.get("body", "")
            print(f"    [{ts}] {author}: {body[:200]}")

    # Events
    if events:
        print(f"\n  Events ({len(events)}):")
        for e in events:
            ek = e.get("kind", "?")
            ts = _fmt_ts(e.get("created_at"))
            run = e.get("run_id")
            r_str = f" (run #{run})" if run else ""
            payload = e.get("payload", "")
            pay_str = f" {payload}" if payload else ""
            print(f"    [{ts}] {ek}{r_str}{pay_str}")

    print()


def _kv(key: str, value: str) -> None:
    """Print a key-value pair."""
    print(f"  {key:<16} : {value}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_task_list(args: argparse.Namespace) -> None:
    """Handle ``task list`` — list tasks with filtering."""
    params: Dict[str, Any] = {}

    if args.status:
        params["status"] = args.status
    if args.assignee:
        params["assignee"] = args.assignee
    if args.tenant:
        params["tenant"] = args.tenant
    if args.parent_id:
        params["parent_id"] = args.parent_id
    if args.q:
        params["q"] = args.q
    params["limit"] = args.limit
    params["offset"] = args.offset
    if args.sort:
        params["sort"] = args.sort

    data = _api_get("/v2/tasks", params=params, server=args.server)

    tasks = data.get("tasks", [])
    total = data.get("total", 0)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    _print_task_table(tasks, total, args.limit, args.offset, args.server)


def cmd_task_show(args: argparse.Namespace) -> None:
    """Handle ``task show <task-id>`` — show full task detail."""
    data = _api_get(f"/v2/tasks/{args.task_id}", server=args.server)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    _print_task_detail(
        task=data.get("task", {}),
        parents=data.get("parents", []),
        children=data.get("children", []),
        runs=data.get("runs", []),
        comments=data.get("comments", []),
        events=data.get("events", []),
    )


# ---------------------------------------------------------------------------
# Parser builder
# ---------------------------------------------------------------------------

# Type alias for the subparsers action (avoids import of argparse internals)
_SubParsersAction = Any


def build_task_parser(subparsers: _SubParsersAction) -> None:
    """Add ``task`` subcommand and its sub-subcommands to the parser."""
    task_parser = subparsers.add_parser(
        "task",
        help="View, search, and filter orchestration tasks",
        description="Manage and inspect orchestration kanban tasks.",
    )
    task_subparsers = task_parser.add_subparsers(
        dest="task_command",
        required=True,
        help="Task subcommand",
    )

    # --- task list ---
    list_parser = task_subparsers.add_parser(
        "list",
        help="List tasks with optional filters",
        description="List orchestration tasks with status, assignee, search, and pagination.",
    )
    list_parser.add_argument(
        "--status",
        default=None,
        help="Filter by status (e.g. 'todo,ready,running' or 'completed')",
    )
    list_parser.add_argument(
        "--assignee",
        default=None,
        help="Filter by assignee profile name",
    )
    list_parser.add_argument(
        "--tenant",
        default=None,
        help="Filter by tenant namespace",
    )
    list_parser.add_argument(
        "--parent-id",
        default=None,
        help="Filter by parent task ID",
    )
    list_parser.add_argument(
        "--q",
        default=None,
        help="Text search in title and body",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max results (default: 50, max: 200)",
    )
    list_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Pagination offset (default: 0)",
    )
    list_parser.add_argument(
        "--sort",
        default=None,
        help="Sort column (prefix with '-' for DESC, e.g. '-created_at', 'priority')",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted table",
    )
    list_parser.set_defaults(func=cmd_task_list)

    # --- task show ---
    show_parser = task_subparsers.add_parser(
        "show",
        help="Show full task detail",
        description="Show complete detail of a single task including runs, comments, events.",
    )
    show_parser.add_argument(
        "task_id",
        help="Task ID to show (e.g. 't_abc12345')",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted output",
    )
    show_parser.set_defaults(func=cmd_task_show)

    # --- shared options (server URL) ---
    for p in (list_parser, show_parser):
        p.add_argument(
            "--server",
            default="http://localhost:8321",
            help="Registry server URL (default: http://localhost:8321)",
        )