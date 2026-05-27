"""A2A Registry SDK usage examples — both sync and async modes.

This example demonstrates the full lifecycle of an A2A agent using the SDK:

1. Connect to the Registry and check health
2. Register with an Agent Card (skills, capabilities, OAuth)
3. Send heartbeats (standalone HTTP)
4. Connect via WebSocket with a dispatch handler
5. Report results/progress for received tasks
6. Poll for task results
7. List agents and tasks
8. Deregister on shutdown

Prerequisites:
    - A running A2A Registry at http://localhost:8321
    - Python packages: ``requests``, ``aiohttp`` (for async mode)

Usage:
    # Sync example (blocking)
    python examples/sdk_usage.py --mode sync --name "My Agent"

    # Async example (uses asyncio)
    python examples/sdk_usage.py --mode async --name "Async Agent"

    # With Registry auth enabled:
    python examples/sdk_usage.py --auth --client-id my-agent --client-secret secret-xxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from typing import Any, Dict, Optional

# Add project root to path for direct script execution
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simple_a2a_registry.client import (
    A2AClient,
    A2AClientError,
    RegistryError,
    NotFoundError,
    AuthError,
    ConnectionError,
)

logger = logging.getLogger("sdk_usage_example")

# ---------------------------------------------------------------------------
# Agent Card definition
# ---------------------------------------------------------------------------

AGENT_NAME = "SDK Example Agent"
AGENT_DESCRIPTION = "An example A2A agent demonstrating the Python SDK client"
AGENT_URL = "http://localhost:9002"
AGENT_SKILLS = [
    {
        "id": "example-greeting",
        "name": "Greeting Service",
        "description": "Responds with a friendly greeting",
        "tags": ["greeting", "example"],
    },
    {
        "id": "example-echo",
        "name": "Echo Service",
        "description": "Echoes back any input text",
        "tags": ["echo", "example"],
    },
]


def _build_agent_card(registry_url: str) -> Dict[str, Any]:
    """Build a complete v1.0 Agent Card for this example agent.

    When using OAuth auth, the card includes an ``OAuth2SecurityScheme``
    so the Registry creates an OAuth client automatically during
    registration.
    """
    card = {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "supported_interfaces": [
            {
                "url": AGENT_URL,
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            },
        ],
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "push_notifications": True,
        },
        "provider": {
            "organization": "A2A SDK Demo",
            "url": "https://example.com/sdk-agent",
        },
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
        "skills": AGENT_SKILLS,
    }
    return card


# ---------------------------------------------------------------------------
# Dispatch handler (called when Registry pushes a task via WebSocket)
# ---------------------------------------------------------------------------


def _on_task(task: Dict[str, Any]) -> None:
    """Handle an incoming task dispatched via WebSocket.

    This is called by the SDK's WebSocket listener when it receives a
    ``{"type": "task", "id": "...", "query": "..."}`` message from the
    Registry.

    The handler receives the raw task payload and should:
    1. Extract the task ID and query
    2. Process the task (this is where real work happens)
    3. Report progress/results via the client

    IMPORTANT: Since this handler runs inside the WebSocket listener's
    asyncio task, it should be fast and non-blocking.  Long-running work
    should be offloaded to a separate thread or task.
    """
    task_id = task.get("id", "")
    query = task.get("query", "") or task.get("body", "") or task.get("title", "")
    logger.info("=== Received WS task '%s': %s ===", task_id, query[:80])

    # The dispatch_handler can use `client` from closure (set up in main)
    # to report progress and results:
    #
    #   client.report_progress(task_id, status="working")
    #
    #   # ... do work ...
    #
    #   client.report_result(task_id, {"text": "done!"})
    #
    # For this example we just log the event — real agents would do
    # substantive processing here.


# ---------------------------------------------------------------------------
# Sync mode example
# ---------------------------------------------------------------------------


def run_sync_example(args: argparse.Namespace) -> None:
    """Run the SDK in synchronous mode using ``requests``.

    Demonstrates:
    1. Creating an A2AClient
    2. Checking registry health
    3. Registering an agent
    4. Heartbeat
    5. Listing agents
    6. Deregistering (cleanup)
    """
    print("\n" + "=" * 60)
    print("  A2A Registry SDK — Sync Mode Example")
    print("=" * 60 + "\n")

    # 1. Create the client
    client = A2AClient(
        registry_url=args.registry,
        client_id=args.client_id,
        client_secret=args.client_secret,
        auth_enabled=args.auth,
        timeout=args.timeout,
    )
    print(f"[OK] Created A2AClient for {args.registry}")

    # 2. Health check
    try:
        health = client.health()
        print(f"[OK] Registry health: {health['status']} "
              f"(v{health['version']}, uptime={health['uptime_seconds']}s)")
    except ConnectionError as e:
        print(f"[FAIL] Registry unreachable: {e}")
        print("  Make sure the A2A Registry is running.")
        return

    # 3. Register the agent
    try:
        agent_card = _build_agent_card(args.registry)
        agent_id = client.register_agent(agent_card=agent_card)
        print(f"[OK] Registered agent as '{agent_id}'")
    except RegistryError as e:
        if e.status == 409:
            print(f"[WARN] Agent already registered (expected on re-run)")
            # Fallback: search for existing agent
            try:
                result = client.list_agents(q=AGENT_NAME)
                agents = result.get("agents", [])
                if agents:
                    agent_id = agents[0]["id"]
                    print(f"[OK] Found existing agent: '{agent_id}'")
                else:
                    print("[FAIL] Could not find existing agent")
                    return
            except Exception as e2:
                print(f"[FAIL] Failed to find agent: {e2}")
                return
        else:
            print(f"[FAIL] Registration failed: {e}")
            return

    # Save agent_id for dispatch example
    args.agent_id = agent_id

    # 4. Heartbeat
    try:
        hb = client.heartbeat(agent_id)
        status = hb.get("status", "?")
        expires = hb.get("expires_at", 0)
        print(f"[OK] Heartbeat sent: status={status}, expires_at={expires}")
    except RegistryError as e:
        print(f"[WARN] Heartbeat failed: {e}")

    # 5. List agents
    try:
        result = client.list_agents()
        print(f"[OK] Registry has {result['total']} agent(s) registered:")
        for agent in result.get("agents", [])[:5]:
            name = agent.get("name", "?")
            aid = agent.get("id", "?")
            conn = agent.get("connection", "http")
            print(f"      - {name} ({aid}) [{conn}]")
    except RegistryError as e:
        print(f"[WARN] List agents failed: {e}")

    # 6. Dispatch a task to self (requires WebSocket connection — not in sync mode)
    print("\n[INFO] Sync mode: WebSocket not available")
    print("  Use --mode async for WebSocket dispatch handling")

    # 7. Cleanup: deregister the agent
    if args.cleanup:
        try:
            result = client.deregister_agent(agent_id)
            print(f"\n[OK] Deregistered agent '{agent_id}'")
        except RegistryError as e:
            if e.status == 404:
                print(f"\n[INFO] Agent '{agent_id}' already removed")
            else:
                print(f"\n[WARN] Deregistration failed: {e}")
    else:
        print(f"\n[SKIP] Cleanup skipped (use --cleanup to deregister)")

    print("\n" + "=" * 60)
    print("  Sync example complete")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Async mode example
# ---------------------------------------------------------------------------


async def run_async_example(args: argparse.Namespace) -> None:
    """Run the SDK in asynchronous mode using ``aiohttp``.

    Demonstrates:
    1. Creating an async A2AClient
    2. Checking registry health
    3. Registering an agent
    4. Connecting via WebSocket with auto-reconnect
    5. Dispatching a task to self
    6. Receiving and handling dispatched tasks
    7. Reporting results
    8. Cleanup
    """
    print("\n" + "=" * 60)
    print("  A2A Registry SDK — Async Mode Example")
    print("=" * 60 + "\n")

    async with A2AClient(
        registry_url=args.registry,
        client_id=args.client_id,
        client_secret=args.client_secret,
        auth_enabled=args.auth,
        timeout=args.timeout,
    ) as client:
        print(f"[OK] Created async A2AClient for {args.registry}")

        # 1. Health check
        try:
            health = await client.async_health()
            print(f"[OK] Registry health: {health['status']} "
                  f"(v{health['version']}, uptime={health['uptime_seconds']}s)")
        except ConnectionError as e:
            print(f"[FAIL] Registry unreachable: {e}")
            return

        # 2. Register the agent
        try:
            agent_card = _build_agent_card(args.registry)
            agent_id = await client.async_register_agent(agent_card=agent_card)
            print(f"[OK] Registered agent as '{agent_id}'")
        except RegistryError as e:
            if e.status == 409:
                print("[WARN] Agent already registered")
                try:
                    result = await client.async_list_agents(q=AGENT_NAME)
                    agents = result.get("agents", [])
                    if agents:
                        agent_id = agents[0]["id"]
                        print(f"[OK] Found existing agent: '{agent_id}'")
                    else:
                        print("[FAIL] Could not find existing agent")
                        return
                except Exception as e2:
                    print(f"[FAIL] Failed to find agent: {e2}")
                    return
            else:
                print(f"[FAIL] Registration failed: {e}")
                return

        args.agent_id = agent_id

        # 3. Heartbeat
        try:
            hb = await client.async_heartbeat(agent_id)
            print(f"[OK] Heartbeat sent: status={hb.get('status', '?')}")
        except RegistryError as e:
            print(f"[WARN] Heartbeat failed: {e}")

        # 4. Set up dispatch handler
        #    The dispatch_handler is called by the WS listener for each
        #    incoming task.  Here we use a simple handler that reports
        #    progress and result immediately.
        async def handle_task(task: Dict[str, Any]) -> None:
            """Async handler for incoming WS tasks."""
            task_id = task.get("id", "?")
            query = task.get("query", "") or task.get("body", "") or task.get("title", "")
            print(f"\n>>> WS TASK RECEIVED <<<")
            print(f"  task_id: {task_id}")
            print(f"  query:   {query[:100]}")

            # Report progress
            await client.async_report_progress(task_id, status="working")
            print(f"  -> reported progress")

            # Simulate work
            await asyncio.sleep(1)

            # Report result
            result_payload = {
                "text": f"Processed: {query}",
                "processing_time_s": 1.0,
            }
            sent = await client.async_report_result(task_id, result_payload)
            if sent:
                print(f"  -> reported result: {result_payload['text']}")
            else:
                print(f"  -> [WARN] WS not connected, result not sent")

        client.dispatch_handler = handle_task
        print("[OK] Dispatch handler set up")

        # 5. Connect WebSocket
        print(f"\nConnecting WebSocket for '{agent_id}'...")
        await client.async_connect_websocket(agent_id)
        print(f"[OK] WebSocket connected (background)")

        # Give the WS connection time to establish
        await asyncio.sleep(1)

        # 6. Dispatch a task to self
        if args.self_dispatch:
            print("\nDispatching a task to self...")
            try:
                result = await client.async_dispatch_task(
                    agent_id,
                    "Hello from the SDK example! Please echo this back.",
                    session_id="demo-session-001",
                )
                print(f"[OK] Dispatched task: {result.get('task_id', '?')}")
                print(f"    state: {result.get('state', '?')}")

                # Wait for the dispatch handler to process it
                await asyncio.sleep(2)
            except RegistryError as e:
                print(f"[WARN] Dispatch failed: {e}")
                if "not connected" in str(e):
                    print("  (The agent needs to be WS-connected first)")
            except Exception as e:
                print(f"[WARN] Dispatch error: {e}")

        # 7. List tasks
        await asyncio.sleep(1)
        try:
            tasks_result = await client.async_task_list(agent_id=agent_id)
            count = tasks_result.get("total", 0)
            print(f"\n[OK] Task list: {count} task(s)")
            for t in tasks_result.get("tasks", [])[:3]:
                print(f"      - {t.get('id', '?')[:12]}... state={t.get('state', '?')}")
        except RegistryError as e:
            print(f"[WARN] List tasks failed: {e}")

        # Let the WS connection breathe for a moment
        print("\n[INFO] WebSocket stays connected (Ctrl+C to exit)")
        print("  The client will auto-reconnect if the connection drops.\n")

        # 8. Wait a bit or stay connected indefinitely
        if args.run_seconds > 0:
            print(f"  Running for {args.run_seconds} seconds...")
            await asyncio.sleep(args.run_seconds)
        else:
            # Wait forever (user interrupts with Ctrl+C)
            try:
                while True:
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass

        # 9. Cleanup (auto-handled by async context manager)

    print("[OK] Client closed (connections cleaned up)")

    if args.cleanup:
        # Re-create client for cleanup (since context manager already closed it)
        async with A2AClient(
            registry_url=args.registry,
            client_id=args.client_id,
            client_secret=args.client_secret,
            auth_enabled=args.auth,
            timeout=args.timeout,
        ) as cleanup_client:
            try:
                result = await cleanup_client.async_deregister_agent(args.agent_id)
                print(f"[OK] Deregistered agent '{args.agent_id}'")
            except RegistryError as e:
                if e.status == 404:
                    print(f"[INFO] Agent '{args.agent_id}' already removed")
                else:
                    print(f"[WARN] Deregistration failed: {e}")

    print("\n" + "=" * 60)
    print("  Async example complete")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A2A Registry SDK usage example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("A2A_REGISTRY_URL", "http://localhost:8321"),
        help="Registry base URL (default: http://localhost:8321, "
             "or $A2A_REGISTRY_URL)",
    )
    parser.add_argument(
        "--mode",
        choices=["sync", "async"],
        default="async",
        help="Run mode: sync (requests) or async (aiohttp) — default: async",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        default=bool(os.environ.get("OAUTH_CLIENT_ID")),
        help="Enable OAuth authentication (default: auto-detect from "
             "OAUTH_CLIENT_ID env var)",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("OAUTH_CLIENT_ID", ""),
        help="OAuth client ID (or $OAUTH_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("OAUTH_CLIENT_SECRET", ""),
        help="OAuth client secret (or $OAUTH_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--self-dispatch",
        action="store_true",
        default=True,
        help="Dispatch a task to self after connecting WS (default: True, "
             "async mode only)",
    )
    parser.add_argument(
        "--no-self-dispatch",
        action="store_false",
        dest="self_dispatch",
        help="Skip self-dispatch test",
    )
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=5,
        help="How long to keep the WS connection alive in async mode "
             "(default: 5; 0 = wait forever)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        default=False,
        help="Deregister the agent on exit (default: False)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"  Registry: {args.registry}")
    print(f"  Auth:     {'enabled' if args.auth else 'disabled'}")
    print(f"  Mode:     {args.mode}")
    if args.auth:
        print(f"  Client:   {args.client_id or '(not set)'}")

    if args.mode == "sync":
        run_sync_example(args)
    else:
        asyncio.run(run_async_example(args))


if __name__ == "__main__":
    main()