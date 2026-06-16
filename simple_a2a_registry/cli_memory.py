"""CLI subcommands for agent memory operations — get, set, list, delete, search.

Usage (via the ``a2a-registry`` entry point)::

    a2a-registry memory get <key> --agent <agent-id> [options]
    a2a-registry memory set <key> --value <json> --agent <agent-id> [options]
    a2a-registry memory list --agent <agent-id> [options]
    a2a-registry memory delete <key> --agent <agent-id> [options]
    a2a-registry memory search <query> --agent <agent-id> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

DEFAULT_SERVER = "http://localhost:8321"

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    server: str = DEFAULT_SERVER,
) -> Dict[str, Any]:
    """Make a GET request to the Registry REST API."""
    if requests is None:
        print(
            "Error: 'requests' library is required. Install with: pip install requests",
            file=sys.stderr,
        )
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


def _api_post(
    path: str,
    json_data: Optional[Dict[str, Any]] = None,
    server: str = DEFAULT_SERVER,
) -> Dict[str, Any]:
    """Make a POST request to the Registry REST API."""
    if requests is None:
        print(
            "Error: 'requests' library is required. Install with: pip install requests",
            file=sys.stderr,
        )
        sys.exit(1)

    url = server.rstrip("/") + path
    try:
        resp = requests.post(url, json=json_data, timeout=30)
    except requests.ConnectionError as e:
        print(f"Error: cannot connect to registry at {server}: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.Timeout:
        print(f"Error: request to {server} timed out", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Error: request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code in (200, 201, 203):
        if resp.text:
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError):
                return {"status": resp.status_code, "text": resp.text}
        return {}
    else:
        try:
            body = resp.json()
            detail = body.get("detail", resp.text)
        except (json.JSONDecodeError, ValueError):
            detail = resp.text
        print(f"Error: {resp.status_code} — {detail}", file=sys.stderr)
        sys.exit(1)


def _api_delete(
    path: str,
    server: str = DEFAULT_SERVER,
) -> Dict[str, Any]:
    """Make a DELETE request to the Registry REST API."""
    if requests is None:
        print(
            "Error: 'requests' library is required. Install with: pip install requests",
            file=sys.stderr,
        )
        sys.exit(1)

    url = server.rstrip("/") + path
    try:
        resp = requests.delete(url, timeout=30)
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
        if resp.text:
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError):
                return {"status": "removed"}
        return {"status": "removed"}
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


def _print_memory_entry(key: str, entry: Dict[str, Any]) -> None:
    """Print a single memory entry in a readable format."""
    value = entry.get("value", entry.get("data", ""))
    namespace = entry.get("namespace", "personal")
    ttl = entry.get("ttl", entry.get("expires_in", None))
    created = entry.get("created_at", entry.get("created", ""))
    updated = entry.get("updated_at", entry.get("updated", ""))

    print(f"  Key:       {key}")
    print(f"  Namespace: {namespace}")
    if ttl:
        print(f"  TTL:       {ttl}s")
    if created:
        print(f"  Created:   {created}")
    if updated:
        print(f"  Updated:   {updated}")
    # Print value in a compact way
    value_str = json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value
    print(f"  Value:     {value_str[:200]}{'…' if len(str(value_str)) > 200 else ''}")
    print()


def _print_memory_table(entries: Dict[str, Any], agent_id: str, namespace: str) -> None:
    """Print memory entries in a compact table format."""
    items = entries.get("items", entries.get("entries", entries.get("memories", [])))
    if isinstance(items, dict):
        items = [{"key": k, **v} for k, v in items.items()]

    if not items:
        print(f"No memories found for agent '{agent_id}' in namespace '{namespace}'.")
        return

    # Column widths
    key_width = max(len(str(item.get("key", ""))) for item in items) + 2
    key_width = max(key_width, 24)
    val_width = 50

    header = (
        f"{'Key':<{key_width}}"
        f"{'Value':<{val_width}}"
        f"{'Namespace':<14}"
        f"{'TTL':<8}"
    )
    print(header)
    print("-" * max(len(header), 80))

    for item in items:
        key = str(item.get("key", "?"))[: key_width - 2]
        raw_val = item.get("value", item.get("data", ""))
        val_str = json.dumps(raw_val, ensure_ascii=False) if not isinstance(raw_val, str) else raw_val
        val_short = val_str[: val_width - 4]
        ns = item.get("namespace", namespace)[:12]
        ttl = str(item.get("ttl", item.get("expires_in", ""))) or "-"

        print(f"{key:<{key_width}}{val_short:<{val_width}}{ns:<14}{ttl:<8}")

    print(f"\nTotal: {len(items)} memory entry(ies)")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_memory_get(args: argparse.Namespace) -> None:
    """Handle ``memory get <key>`` — retrieve a memory entry."""
    params: Dict[str, Any] = {"key": args.key}
    if args.namespace:
        params["namespace"] = args.namespace

    data = _api_get(f"/v2/memory/{args.agent_id}", params=params, server=args.server)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    entry = data.get("entry", data.get("memory", data))
    if entry:
        print(f"Memory entry for key '{args.key}' (agent: {args.agent_id}):\n")
        _print_memory_entry(args.key, entry)
    else:
        print(f"No memory found for key '{args.key}' on agent '{args.agent_id}'.")


def cmd_memory_set(args: argparse.Namespace) -> None:
    """Handle ``memory set <key> --value <json>`` — store a memory entry."""
    try:
        parsed_value = json.loads(args.value)
    except (json.JSONDecodeError, ValueError):
        parsed_value = args.value

    payload: Dict[str, Any] = {
        "key": args.key,
        "value": parsed_value,
    }
    if args.namespace:
        payload["namespace"] = args.namespace
    if args.ttl is not None:
        payload["ttl"] = args.ttl

    data = _api_post(f"/v2/memory/{args.agent_id}", json_data=payload, server=args.server)

    entry = data.get("entry", data.get("memory", data))
    print(f"Memory set successfully for key '{args.key}' on agent '{args.agent_id}'.")

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif entry:
        _print_memory_entry(args.key, entry)


def cmd_memory_list(args: argparse.Namespace) -> None:
    """Handle ``memory list`` — list memory entries for an agent."""
    params: Dict[str, Any] = {}
    if args.namespace:
        params["namespace"] = args.namespace
    if args.prefix:
        params["prefix"] = args.prefix
    if args.limit is not None:
        params["limit"] = args.limit

    data = _api_get(f"/v2/memory/{args.agent_id}", params=params, server=args.server)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    namespace = args.namespace or "personal"
    _print_memory_table(data, args.agent_id, namespace)


def cmd_memory_delete(args: argparse.Namespace) -> None:
    """Handle ``memory delete <key>`` — remove a memory entry."""
    # Build query string for params since _api_delete doesn't accept params
    query_parts = [f"key={args.key}"]
    if args.namespace:
        query_parts.append(f"namespace={args.namespace}")
    query_str = "?" + "&".join(query_parts)

    data = _api_delete(f"/v2/memory/{args.agent_id}{query_str}", server=args.server)

    print(f"Memory entry '{args.key}' deleted from agent '{args.agent_id}'.")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_memory_search(args: argparse.Namespace) -> None:
    """Handle ``memory search <query>`` — semantic search over memory."""
    payload: Dict[str, Any] = {"query": args.query}
    if args.namespace:
        payload["namespace"] = args.namespace

    data = _api_post(
        f"/v2/memory/{args.agent_id}/search",
        json_data=payload,
        server=args.server,
    )

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    results = data.get("results", data.get("items", data.get("memories", [])))
    if not results:
        print(f"No results found for query '{args.query}' on agent '{args.agent_id}'.")
        return

    namespace = args.namespace or "personal"
    print(f"Search results for '{args.query}' (agent: {args.agent_id}, namespace: {namespace}):\n")

    if isinstance(results, list):
        for i, item in enumerate(results, 1):
            key = item.get("key", f"result_{i}")
            score = item.get("score", item.get("relevance", ""))
            print(f"  [{i}] Key: {key}" + (f"  (score: {score})" if score else ""))
            _print_memory_entry(key, item)
    else:
        _print_memory_table({"items": results}, args.agent_id, namespace)
    print(f"Total: {len(results) if isinstance(results, list) else '?'} result(s)")


# ---------------------------------------------------------------------------
# Parser builder
# ---------------------------------------------------------------------------

# Type alias for the subparsers action (avoids import of argparse internals)
_SubParsersAction = Any


def build_memory_parser(subparsers: _SubParsersAction) -> None:
    """Add ``memory`` subcommand and its sub-subcommands to the parser."""
    memory_parser = subparsers.add_parser(
        "memory",
        help="Manage agent memory (get, set, list, delete, search)",
        description="CLI for agent memory operations — store and retrieve key-value memory entries.",
    )
    memory_subparsers = memory_parser.add_subparsers(
        dest="memory_command",
        required=True,
        help="Memory subcommand",
    )

    # --- memory get ---
    get_parser = memory_subparsers.add_parser(
        "get",
        aliases=["show", "read"],
        help="Get a memory entry by key",
        description="Retrieve a single memory entry by its key for the given agent.",
    )
    get_parser.add_argument("key", help="Memory key to retrieve")
    get_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    get_parser.add_argument("--namespace", "-n", default="personal", help="Memory namespace (default: personal)")
    get_parser.add_argument("--json", action="store_true", default=False,
                            help="Output raw JSON instead of formatted output")
    get_parser.set_defaults(func=cmd_memory_get)

    # --- memory set ---
    set_parser = memory_subparsers.add_parser(
        "set",
        aliases=["put", "store"],
        help="Set a memory entry",
        description="Store a key-value memory entry for an agent with optional TTL.",
    )
    set_parser.add_argument("key", help="Memory key to set")
    set_parser.add_argument("--value", "-v", required=True, help="Memory value (JSON string)")
    set_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    set_parser.add_argument("--ttl", "-t", type=int, default=None,
                            help="Time-to-live in seconds (default: no expiry)")
    set_parser.add_argument("--namespace", "-n", default="personal", help="Memory namespace (default: personal)")
    set_parser.add_argument("--json", action="store_true", default=False,
                            help="Output raw JSON response")
    set_parser.set_defaults(func=cmd_memory_set)

    # --- memory list ---
    list_parser = memory_subparsers.add_parser(
        "list",
        aliases=["ls", "all"],
        help="List memory entries",
        description="List all memory entries for an agent, with optional prefix filter and limit.",
    )
    list_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    list_parser.add_argument("--namespace", "-n", default="personal", help="Memory namespace (default: personal)")
    list_parser.add_argument("--prefix", "-p", default="", help="Filter by key prefix")
    list_parser.add_argument("--limit", "-l", type=int, default=None,
                             help="Maximum number of entries to return")
    list_parser.add_argument("--json", action="store_true", default=False,
                             help="Output raw JSON instead of formatted table")
    list_parser.set_defaults(func=cmd_memory_list)

    # --- memory delete ---
    del_parser = memory_subparsers.add_parser(
        "delete",
        aliases=["del", "remove", "rm"],
        help="Delete a memory entry",
        description="Delete a single memory entry by key for the given agent.",
    )
    del_parser.add_argument("key", help="Memory key to delete")
    del_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    del_parser.add_argument("--namespace", "-n", default="personal", help="Memory namespace (default: personal)")
    del_parser.add_argument("--json", action="store_true", default=False,
                            help="Output raw JSON response")
    del_parser.set_defaults(func=cmd_memory_delete)

    # --- memory search ---
    search_parser = memory_subparsers.add_parser(
        "search",
        aliases=["find", "query"],
        help="Search memory entries",
        description="Semantic/full-text search over memory entries for an agent.",
    )
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    search_parser.add_argument("--namespace", "-n", default="personal", help="Memory namespace (default: personal)")
    search_parser.add_argument("--json", action="store_true", default=False,
                               help="Output raw JSON instead of formatted output")
    search_parser.set_defaults(func=cmd_memory_search)

    # --- server URL on each subcommand (matches cli_agent.py pattern) ---
    for sp in (get_parser, set_parser, list_parser, del_parser, search_parser):
        sp.add_argument(
            "--server",
            default=DEFAULT_SERVER,
            help=f"Registry server URL (default: {DEFAULT_SERVER})",
        )