"""CLI subcommands for audit log history — view/search/filter audit events.

Usage (via the ``a2a-registry`` entry point)::

    a2a-registry history list [options]
    a2a-registry history show <event-id> [options]
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


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_ts(ts: Optional[float]) -> str:
    """Format a unix timestamp to a readable string."""
    if ts is None:
        return "-"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(ts)


def _event_icon(event_type: str) -> str:
    """Return a visual icon for the audit event type."""
    icons = {
        "CLIENT_CREATE": "⊕",
        "CLIENT_DELETE": "⊖",
        "AGENT_REGISTER": "◈",
        "AGENT_DEREGISTER": "◇",
        "TOKEN_ISSUE": "🔑",
        "TASK_DISPATCH": "▶",
        "CONFIG_CHANGE": "⚙",
        "AUTH_FAILURE": "✗",
    }
    return icons.get(event_type, "?")


def _print_event_table(events: List[Dict[str, Any]], total: int, limit: int,
                        offset: int, server: str) -> None:
    """Print audit events in a compact table format."""
    if not events:
        print("No audit events found.")
        return

    # Column widths
    id_width = 14
    ts_width = 24
    type_width = max(len(e.get("event_type", "")) for e in events) + 4
    actor_width = max(len(e.get("actor", "") or "-") for e in events) + 2
    target_width = max(len(e.get("target", "") or "-") for e in events) + 2
    success_width = 10

    # Header
    header = (
        f"{'ID':<{id_width}}"
        f"{'Timestamp':<{ts_width}}"
        f"{'Event Type':<{type_width}}"
        f"{'Actor':<{actor_width}}"
        f"{'Target':<{target_width}}"
        f"{'Success':<{success_width}}"
    )
    print(header)
    print("-" * len(header))

    for e in events:
        icon = _event_icon(e.get("event_type", ""))
        ts = _fmt_ts(e.get("timestamp"))
        et = f"{icon} {e.get('event_type', '?')}"
        actor = e.get("actor") or "-"
        target = e.get("target") or "-"
        success = "✓" if e.get("success", True) else "✗"

        print(
            f"{str(e.get('id', '?')):<{id_width}}"
            f"{ts:<{ts_width}}"
            f"{et:<{type_width}}"
            f"{actor:<{actor_width}}"
            f"{target:<{target_width}}"
            f"{success:<{success_width}}"
        )

    # Footer
    shown = len(events)
    print(f"\nShowing {shown} of {total} events (offset={offset}, limit={limit})")
    if total > offset + limit:
        next_offset = offset + limit
        print(f"Use --offset {next_offset} to see next page.")


def _print_event_detail(event: Dict[str, Any]) -> None:
    """Print full detail of a single audit event."""
    icon = _event_icon(event.get("event_type", ""))
    print(f"\n{'=' * 60}")
    print(f"  {icon}  Event #{event.get('id', '?')} — {event.get('event_type', '?')}")
    print(f"{'=' * 60}")

    _kv("ID", str(event.get("id", "?")))
    _kv("Event Type", event.get("event_type", "?"))
    _kv("Timestamp", _fmt_ts(event.get("timestamp")))
    _kv("Actor", event.get("actor", "-"))
    _kv("Target", event.get("target", "-"))
    _kv("Success", "✓ Yes" if event.get("success", True) else "✗ No")
    _kv("Tenant", str(event.get("tenant_id", "") or "-"))

    detail = event.get("detail", "")
    if detail:
        print(f"\n  Detail:")
        # Try to pretty-print JSON detail
        try:
            parsed = json.loads(detail)
            for line in json.dumps(parsed, indent=2, ensure_ascii=False).split("\n"):
                print(f"    {line}")
        except (json.JSONDecodeError, ValueError, TypeError):
            for line in detail.strip().split("\n"):
                print(f"    {line}")

    print()


def _kv(key: str, value: str) -> None:
    """Print a key-value pair."""
    print(f"  {key:<16} : {value}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_history_list(args: argparse.Namespace) -> None:
    """Handle ``history list`` — list audit events with filtering."""
    params: Dict[str, Any] = {}

    if args.event_type:
        params["event_type"] = args.event_type
    if args.actor:
        params["actor"] = args.actor
    if args.since is not None:
        params["since"] = args.since
    if args.until is not None:
        params["until"] = args.until
    params["limit"] = args.limit
    params["offset"] = args.offset

    data = _api_get("/admin/audit", params=params, server=args.server)

    events = data.get("events", [])
    total = data.get("total", 0)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    _print_event_table(events, total, args.limit, args.offset, args.server)

    # Print stats summary when no filters are active
    stats = data.get("stats", {})
    if stats:
        print(f"\n  Audit Log Stats:")
        print(f"    Total events:     {stats.get('total_events', '?')}")
        oldest = _fmt_ts(stats.get("oldest_timestamp"))
        newest = _fmt_ts(stats.get("newest_timestamp"))
        print(f"    Oldest event:     {oldest}")
        print(f"    Newest event:     {newest}")
        by_type = stats.get("by_event_type", {})
        if by_type:
            print(f"    By event type:")
            for et, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
                icon = _event_icon(et)
                print(f"      {icon}  {et:<20s} {cnt}")


def cmd_history_show(args: argparse.Namespace) -> None:
    """Handle ``history show <event-id>`` — show a single audit event detail.

    Queries the audit log via /admin/audit with limit=1000 and filters
    client-side to find the event by ID. This works because audit events
    have sequential IDs and the audit endpoint returns ordered results.
    """
    event_id = args.event_id

    # Convert event_id to int for lookup
    try:
        target_id = int(event_id)
    except (ValueError, TypeError):
        print(f"Error: event ID must be an integer, got '{event_id}'", file=sys.stderr)
        sys.exit(1)

    # Fetch and filter client-side — the audit endpoint orders by timestamp DESC
    # so we need to handle any ordering. Use a generous limit.
    data = _api_get("/admin/audit", params={"limit": 1000, "offset": 0}, server=args.server)
    events = data.get("events", [])

    target_event: Optional[Dict[str, Any]] = None
    for e in events:
        if e.get("id") == target_id:
            target_event = e
            break

    if target_event is None:
        print(f"Error: event #{event_id} not found (searched last 1000 events)", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(target_event, indent=2, ensure_ascii=False))
        return

    _print_event_detail(target_event)


# ---------------------------------------------------------------------------
# Parser builder
# ---------------------------------------------------------------------------


def build_history_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add ``history`` subcommand and its sub-subcommands to the parser."""
    history_parser = subparsers.add_parser(
        "history",
        help="Query audit log timeline events",
        description="Inspect audit log entries with optional time-range, type, and actor filters.",
    )

    # Common server argument
    history_parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Registry server URL (default: {DEFAULT_SERVER})",
    )

    history_subparsers = history_parser.add_subparsers(
        dest="history_command",
        required=True,
        help="History subcommand",
    )

    # --- history list ---
    list_parser = history_subparsers.add_parser(
        "list",
        help="List audit events with optional filters",
        description=(
            "List audit log entries with event type, actor, time-range, "
            "and pagination filters."
        ),
    )
    list_parser.add_argument(
        "--event-type",
        default=None,
        help=(
            "Filter by event type (e.g. 'AGENT_REGISTER', 'TASK_DISPATCH', "
            "'AUTH_FAILURE')"
        ),
    )
    list_parser.add_argument(
        "--actor",
        default=None,
        help="Filter by actor (substring match, case-insensitive)",
    )
    list_parser.add_argument(
        "--since",
        type=float,
        default=None,
        help=(
            "Filter by start time — Unix timestamp. "
            "Only events at or after this time."
        ),
    )
    list_parser.add_argument(
        "--until",
        type=float,
        default=None,
        help=(
            "Filter by end time — Unix timestamp. "
            "Only events before this time."
        ),
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max results (default: 100, max: 1000)",
    )
    list_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Pagination offset (default: 0)",
    )
    list_parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Registry server URL (default: {DEFAULT_SERVER})",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted table",
    )
    list_parser.set_defaults(func=cmd_history_list)

    # --- history show ---
    show_parser = history_subparsers.add_parser(
        "show",
        help="Show a single audit event detail",
        description="Show complete detail of a single audit event by its event ID.",
    )
    show_parser.add_argument(
        "event_id",
        help="Audit event ID to show (numeric ID, e.g. '42')",
    )
    show_parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Registry server URL (default: {DEFAULT_SERVER})",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted output",
    )
    show_parser.set_defaults(func=cmd_history_show)