# Simple A2A Registry — API Reference

## Base URL

Default: `http://localhost:8321`

## Endpoints

### Health Check

```
GET /health
```

Returns server health status and stats.

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime_seconds": 123.45,
  "stats": {
    "total_agents": 5,
    "alive_agents": 4,
    "stale_agents": 1,
    "external_agents": 3,
    "discovered_agents": 2
  }
}
```

### Well-Known Agent Card

```
GET /.well-known/agent-card.json
```

Returns the registry's own A2A Agent Card.

### List Agents

```
GET /v1/agents
GET /v1/agents?skill=<name>
GET /v1/agents?tag=<tag>
GET /v1/agents?q=<search>
GET /v1/agents?limit=20&offset=0
```

**Query Parameters:**
- `skill` - Filter by skill name (substring match)
- `tag` - Filter by exact tag match
- `q` - Full-text search across all fields
- `limit` - Max results (default 50, max 200)
- `offset` - Pagination offset (default 0)

### Get Agent

```
GET /v1/agents/{agent_id}
```

### Register Agent

```
POST /v1/agents
```

**Body:** A2A Agent Card JSON with at least a `name` field.

**Response:** `201 Created` with the assigned `id` and full card.

**Errors:**
- `400` - Missing or empty name, invalid JSON
- `409` - Duplicate name

### Heartbeat

```
POST /v1/agents/{agent_id}/heartbeat
```

**Response:** `203 Non-Authoritative Information`

```json
{
  "id": "agent-uuid",
  "status": "alive",
  "last_heartbeat": 1712345678.9,
  "expires_at": 1712345798.9,
  "stale_timeout": 120
}
```

**Errors:**
- `404` - Agent not found
- `410` - Agent is stale

### Unregister Agent

```
DELETE /v1/agents/{agent_id}
```

### Filesystem Discovery

```
POST /v1/discover[?profiles_home=<path>]
```

Scans a Hermes Agent profile directory for local agents and skills.

### Proxy Task (Experimental)

```
POST /v1/agents/{agent_id}/task
```

Returns the agent's target URL for task routing.

## Port

Default port: **8321**