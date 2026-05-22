# Simple A2A Registry — Architecture

## Overview

The Simple A2A Registry is an HTTP-based discovery and registration service for A2A-compatible agents. It combines two agent sources:

1. **External agents** - registered via API with heartbeat-based liveness
2. **Discovered agents** - scanned from local Hermes Agent profile directories

## Components

```
simple_a2a_registry/
  cli.py          — argparse CLI entry point
  server.py       — aiohttp REST API + app factory
  store.py        — persistent registry state (JSON file)
  models.py       — A2A Agent Card data models
  discovery.py    — filesystem profile/skill scanner
  static/         — web dashboard (HTML+JS)
tests/
  test_store.py   — unit tests for store
  test_models.py  — unit tests for models
  test_server.py  — integration tests for HTTP API
```

## Data Flow

```
         ┌───────────────┐
         │  External Agent│
         └──┬────────────┘
            │ POST /v1/agents + heartbeat every 2min
            ▼
    ┌───────────────┐         ┌──────────────┐
    │  A2A Registry  │────────▶ registry.json │
    │  (aiohttp)     │         └──────────────┘
    └───┬───────┬────┘
        │       │
        │       └─────── POST /v1/discover
        ▼
    ┌───────────────┐
    │ Hermes Profiles│  (profiles/ + skills/)
    └───────────────┘
```

## Key Design Decisions

- **Pydantic-free**: Data classes with manual dict serialization — zero dependencies beyond `aiohttp`
- **JSON persistence**: Atomic writes via temp file + replace
- **Heartbeat model**: 120s timeout, 300s purge
- **Discovery read-only**: Scanned agents can't be unregistered via API
- **No auth**: Designed for local/trusted networks