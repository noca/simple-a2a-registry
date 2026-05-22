# Simple A2A Registry

A lightweight, spec-compliant A2A (Agent-to-Agent) Registry server.

## Quick Start

```
pip install simple-a2a-registry
a2a-registry
```

Open http://localhost:8321 for the dashboard.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/.well-known/agent-card.json` | Registry's own A2A Agent Card |
| GET | `/v1/agents` | List/search agents |
| GET | `/v1/agents/{id}` | Get agent details |
| POST | `/v1/agents` | Register an agent |
| DELETE | `/v1/agents/{id}` | Unregister an agent |
| POST | `/v1/agents/{id}/heartbeat` | Send heartbeat |
| POST | `/v1/discover` | Trigger filesystem discovery scan |
| POST | `/v1/agents/{id}/task` | Proxy task to agent |

## CLI

```
a2a-registry --host 0.0.0.0 --port 8321 --data-dir ~/.simple-a2a-registry
```

## License

MIT