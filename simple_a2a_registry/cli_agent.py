"""CLI subcommands for agent operations — register, list, status management.

Usage (via the ``a2a-registry`` entry point)::

    a2a-registry agent list [options]
    a2a-registry agent get <agent-id>
    a2a-registry agent register <name> [options]
    a2a-registry agent unregister <agent-id>
    a2a-registry agent heartbeat <agent-id>
    a2a-registry agent toggle <agent-id>
    a2a-registry agent stats
    a2a-registry agent purge-stale
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


def _api_post(
    path: str,
    json_data: Optional[Dict[str, Any]] = None,
    server: str = DEFAULT_SERVER,
) -> Dict[str, Any]:
    """Make a POST request to the Registry REST API."""
    if requests is None:
        print("Error: 'requests' library is required. Install with: pip install requests",
              file=sys.stderr)
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
        print("Error: 'requests' library is required. Install with: pip install requests",
              file=sys.stderr)
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


def _fmt_ts(ts: Optional[float]) -> str:
    """Format a unix timestamp to a readable string."""
    if ts is None or ts == 0:
        return "-"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(ts)


def _fmt_age(ts: float) -> str:
    """Format a timestamp as human-readable age."""
    if not ts:
        return "-"
    elapsed = time.time() - ts
    if elapsed < 60:
        return f"{elapsed:.0f}s ago"
    elif elapsed < 3600:
        return f"{elapsed / 60:.0f}m ago"
    elif elapsed < 86400:
        return f"{elapsed / 3600:.1f}h ago"
    else:
        return f"{elapsed / 86400:.1f}d ago"


def _status_icon(status: str) -> str:
    """Return a small visual icon for the agent status."""
    icons = {
        "alive": "●",
        "stale": "◌",
        "disabled": "⊘",
        "unknown": "?",
    }
    return icons.get(status, "?")


def _print_agent_table(agents: List[Dict[str, Any]]) -> None:
    """Print agents in a compact table format."""
    if not agents:
        print("No agents found.")
        return

    # Column widths
    id_width = max(len(a.get("id", "")) for a in agents) + 2
    id_width = max(id_width, 38)
    name_width = max(len(a.get("name", "")[:24]) for a in agents) + 2
    name_width = max(name_width, 20)

    # Header
    header = (
        f"{'ID':<{id_width}}"
        f"{'Name':<{name_width}}"
        f"{'Status':<12}"
        f"{'HB Age':<14}"
        f"{'Tenant':<16}"
        f"{'Channel'}"
    )
    print(header)
    print("-" * max(len(header), 80))

    for a in agents:
        aid = a.get("id", "?")[:id_width - 2]
        name = a.get("name", "?")[:name_width - 2]
        status = a.get("status", "unknown")
        icon = _status_icon(status)
        hb = a.get("lastHeartbeat", 0)
        hb_str = _fmt_age(hb) if hb else "-"
        tenant = a.get("tenant", "") or "-"
        channel = a.get("preferred_channel", "ws") or "ws"

        print(
            f"{aid:<{id_width}}"
            f"{name:<{name_width}}"
            f"{icon} {status:<8}"
            f"{hb_str:<14}"
            f"{tenant:<16}"
            f"{channel}"
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_agent_list(args: argparse.Namespace) -> None:
    """Handle ``agent list`` — list registered agents."""
    params: Dict[str, Any] = {}
    if args.skill:
        params["skill"] = args.skill
    if args.tag:
        params["tag"] = args.tag
    if args.query:
        params["q"] = args.query
    if args.tenant:
        params["tenant"] = args.tenant

    data = _api_get("/v1/agents", params=params, server=args.server)
    agents = data.get("agents", data if isinstance(data, list) else [])

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    _print_agent_table(agents)
    print(f"\nTotal: {len(agents)} agent(s)")


def cmd_agent_get(args: argparse.Namespace) -> None:
    """Handle ``agent get <agent-id>`` — show agent details."""
    data = _api_get(f"/v1/agents/{args.agent_id}", server=args.server)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    agent = data
    icon = _status_icon(agent.get("status", ""))
    print(f"\n  {icon}  {agent.get('name', '(unnamed)')}")
    print("  " + "=" * 58)

    print(f"  {'ID':<18} : {args.agent_id}")
    print(f"  {'Name':<18} : {agent.get('name', '?')}")
    print(f"  {'Description':<18} : {agent.get('description', '')}")
    print(f"  {'Status':<18} : {agent.get('status', 'unknown')}")
    print(f"  {'Version':<18} : {agent.get('version', '?')}")
    print(f"  {'Tenant':<18} : {agent.get('tenant', '') or '(none)'}")
    print(f"  {'Channel':<18} : {agent.get('preferred_channel', 'ws')}")
    print(f"  {'Disabled':<18} : {agent.get('disabled', False)}")
    last_hb = agent.get("lastHeartbeat", 0)
    print(f"  {'Last Heartbeat':<18} : {_fmt_ts(last_hb)} ({_fmt_age(last_hb)})")

    provider = agent.get("provider")
    if provider:
        org = provider.get("organization", "?")
        url = provider.get("url", "?")
        print(f"  {'Provider':<18} : {org} ({url})")

    interfaces = agent.get("supported_interfaces", [])
    if interfaces:
        print(f"  {'Interfaces':<18} : {len(interfaces)} endpoint(s)")
        for iface in interfaces:
            url = iface.get("url", "?")
            proto = iface.get("protocol_binding", "?")
            ver = iface.get("protocol_version", "?")
            print(f"    - {proto} v{ver} @ {url}")

    skills = agent.get("skills", [])
    if skills:
        print(f"  {'Skills':<18} : {len(skills)}")
        for s in skills:
            sid = s.get("id", "?")
            sname = s.get("name", "?")
            print(f"    - {sname} ({sid})")

    print()


def cmd_agent_register(args: argparse.Namespace) -> None:
    """Handle ``agent register <name>`` — register a new agent."""
    payload: Dict[str, Any] = {
        "name": args.name,
        "description": args.description or "",
    }
    if args.url:
        payload["supported_interfaces"] = [
            {
                "url": args.url,
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }
        ]
    if args.tenant:
        payload["tenant"] = args.tenant
    if args.card_file:
        try:
            with open(args.card_file, "r", encoding="utf-8") as f:
                card_data = json.load(f)
            # Merge: card_file overrides individual params
            card_data.update(payload)
            payload = card_data
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error reading card file: {e}", file=sys.stderr)
            sys.exit(1)

    data = _api_post("/v1/agents", json_data=payload, server=args.server)
    agent_id = data.get("id", "?")
    print(f"Agent registered successfully.")
    print(f"  ID:   {agent_id}")
    print(f"  Name: {args.name}")
    if args.url:
        print(f"  URL:  {args.url}")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_agent_unregister(args: argparse.Namespace) -> None:
    """Handle ``agent unregister <agent-id>`` — remove an agent."""
    data = _api_delete(f"/v1/agents/{args.agent_id}", server=args.server)
    print(f"Agent '{args.agent_id}' removed.")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_agent_heartbeat(args: argparse.Namespace) -> None:
    """Handle ``agent heartbeat <agent-id>`` — send heartbeat."""
    data = _api_post(f"/v1/agents/{args.agent_id}/heartbeat", server=args.server)
    expires = data.get("expires_at", 0)
    status = data.get("status", "?")
    print(f"Heartbeat sent for agent '{args.agent_id}'.")
    print(f"  Status:  {status}")
    print(f"  Expires: {_fmt_ts(expires)}")


def cmd_agent_toggle(args: argparse.Namespace) -> None:
    """Handle ``agent toggle <agent-id>`` — enable/disable an agent."""
    data = _api_post(f"/v1/agents/{args.agent_id}/toggle", server=args.server)
    now_disabled = data.get("disabled", data.get("status") == "disabled")
    state = "disabled" if now_disabled else "enabled"
    print(f"Agent '{args.agent_id}' is now {state}.")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_agent_stats(args: argparse.Namespace) -> None:
    """Handle ``agent stats`` — show agent statistics."""
    params: Dict[str, Any] = {}
    if args.tenant:
        params["tenant"] = args.tenant

    data = _api_get("/v1/agents", params=params, server=args.server)
    agents = data.get("agents", data if isinstance(data, list) else [])

    total = len(agents)
    alive = sum(1 for a in agents if a.get("status") == "alive")
    stale = sum(1 for a in agents if a.get("status") == "stale")
    disabled = sum(1 for a in agents if a.get("disabled") or a.get("status") == "disabled")

    print(f"Agent Statistics{' (tenant: ' + args.tenant + ')' if args.tenant else ''}")
    print(f"  {'Total':<20} : {total}")
    print(f"  {'Alive':<20} : {alive}")
    print(f"  {'Stale':<20} : {stale}")
    print(f"  {'Disabled':<20} : {disabled}")

    # Tenant breakdown
    tenants: Dict[str, int] = {}
    for a in agents:
        t = a.get("tenant", "") or "(none)"
        tenants[t] = tenants.get(t, 0) + 1
    if len(tenants) > 1:
        print(f"\n  By tenant:")
        for t, count in sorted(tenants.items(), key=lambda x: -x[1]):
            print(f"    {t:<20} : {count}")


def cmd_agent_purge_stale(args: argparse.Namespace) -> None:
    """Handle ``agent purge-stale`` — remove stale agents."""
    data = _api_get("/v1/agents", server=args.server)
    agents = data.get("agents", data if isinstance(data, list) else [])
    stale = [a for a in agents if a.get("status") == "stale"]

    if not stale:
        print("No stale agents to purge.")
        return

    removed = 0
    for a in stale:
        aid = a.get("id", "")
        _api_delete(f"/v1/agents/{aid}", server=args.server)
        removed += 1
        aid_short = aid[:20]
        print(f"  Removed: {aid_short}... ({a.get('name', '?')})")

    print(f"\nPurged {removed} stale agent(s).")


# ---------------------------------------------------------------------------
# Parser builder
# ---------------------------------------------------------------------------

# Type alias for the subparsers action (avoids import of argparse internals)
_SubParsersAction = Any


def build_agent_parser(subparsers: _SubParsersAction) -> None:
    """Add ``agent`` subcommand and its sub-subcommands to the parser."""
    agent_parser = subparsers.add_parser(
        "agent",
        help="Manage agents (list, get, register, unregister, etc.)",
        description="Admin CLI for agent registration and status management.",
    )
    agent_subparsers = agent_parser.add_subparsers(
        dest="agent_command",
        required=True,
        help="Agent subcommand",
    )

    # --- agent list ---
    list_parser = agent_subparsers.add_parser(
        "list",
        help="List registered agents",
        description="List all registered agents with status, heartbeat, and filters.",
    )
    list_parser.add_argument("--skill", default="", help="Filter by skill name/ID")
    list_parser.add_argument("--tag", default="", help="Filter by tag")
    list_parser.add_argument("--query", "-q", default="", help="Full-text search query")
    list_parser.add_argument("--tenant", default=None, help="Filter by tenant")
    list_parser.add_argument("--json", action="store_true", default=False,
                             help="Output raw JSON instead of formatted table")
    list_parser.set_defaults(func=cmd_agent_list)

    # --- agent get ---
    get_parser = agent_subparsers.add_parser(
        "get",
        aliases=["show"],
        help="Show agent details",
        description="Show complete details of a single registered agent.",
    )
    get_parser.add_argument("agent_id", help="Agent ID to inspect")
    get_parser.add_argument("--json", action="store_true", default=False,
                            help="Output raw JSON instead of formatted output")
    get_parser.set_defaults(func=cmd_agent_get)

    # --- agent register ---
    reg_parser = agent_subparsers.add_parser(
        "register",
        help="Register a new agent",
        description="Register a new agent with the A2A Registry.",
    )
    reg_parser.add_argument("name", help="Agent display name")
    reg_parser.add_argument("--description", "-d", default="", help="Agent description")
    reg_parser.add_argument("--url", "-u", default="", help="Agent endpoint URL")
    reg_parser.add_argument("--tenant", default=None, help="Tenant namespace")
    reg_parser.add_argument("--card-file", "-c", default="",
                            help="Path to agent card JSON file (overrides individual params)")
    reg_parser.add_argument("--json", action="store_true", default=False,
                            help="Output raw JSON response")
    reg_parser.set_defaults(func=cmd_agent_register)

    # --- agent unregister ---
    unreg_parser = agent_subparsers.add_parser(
        "unregister",
        help="Remove an agent",
        description="Remove (deregister) an agent from the Registry.",
    )
    unreg_parser.add_argument("agent_id", help="Agent ID to remove")
    unreg_parser.add_argument("--json", action="store_true", default=False,
                              help="Output raw JSON response")
    unreg_parser.set_defaults(func=cmd_agent_unregister)

    # --- agent heartbeat ---
    hb_parser = agent_subparsers.add_parser(
        "heartbeat",
        help="Send heartbeat for an agent",
        description="Send a heartbeat for an agent to keep it alive.",
    )
    hb_parser.add_argument("agent_id", help="Agent ID to heartbeat")
    hb_parser.set_defaults(func=cmd_agent_heartbeat)

    # --- agent toggle ---
    toggle_parser = agent_subparsers.add_parser(
        "toggle",
        help="Enable/disable an agent",
        description="Toggle the disabled status of an agent (enable ↔ disable).",
    )
    toggle_parser.add_argument("agent_id", help="Agent ID to toggle")
    toggle_parser.add_argument("--json", action="store_true", default=False,
                               help="Output raw JSON response")
    toggle_parser.set_defaults(func=cmd_agent_toggle)

    # --- agent stats ---
    stats_parser = agent_subparsers.add_parser(
        "stats",
        help="Show agent statistics",
        description="Show aggregated statistics about all registered agents.",
    )
    stats_parser.add_argument("--tenant", default=None, help="Filter by tenant")
    stats_parser.set_defaults(func=cmd_agent_stats)

    # --- agent purge-stale ---
    purge_parser = agent_subparsers.add_parser(
        "purge-stale",
        help="Remove stale agents",
        description="Find and remove all stale agents (no recent heartbeat).",
    )
    purge_parser.set_defaults(func=cmd_agent_purge_stale)

    # --- server URL on each subcommand (matches cli_task.py pattern) ---
    for sp in (list_parser, get_parser, reg_parser, unreg_parser,
               hb_parser, toggle_parser, stats_parser, purge_parser):
        sp.add_argument(
            "--server",
            default=DEFAULT_SERVER,
            help=f"Registry server URL (default: {DEFAULT_SERVER})",
        )