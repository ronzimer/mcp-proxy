# MCP Smart Proxy

A lightweight **Model Context Protocol (MCP) proxy** that sits between LLM clients (e.g. Claude Desktop)
and multiple upstream MCP servers, providing **caching, latency measurement, and request auditing**.

The proxy is designed to be **generic**, **extensible**, and **server-agnostic**, allowing multiple MCP
servers (e.g. Wikipedia, weather APIs) to be connected without server-specific logic.

---

## Architecture Overview

The system consists of two main components:

### 1. Proxy Core (`proxy_core.py`)
The central TCP server that:
- Accepts MCP requests from endpoints
- Computes cache keys
- Handles TTL-based caching
- Forwards requests to upstream MCP servers
- Measures latency
- Logs all requests to SQLite

### 2. Proxy Endpoint (`proxy_endpoint.py`)
A thin client-side bridge that:
- Connects an MCP-compatible client to the proxy-core
- Forwards MCP JSON-RPC messages
- Identifies the upstream server (`server_id`)
- Provides a `caller_id` for logging and cache analysis

---

## Request Flow (High-Level)

1. Client sends a request → proxy endpoint
2. Endpoint forwards request → proxy core
3. Proxy core:
   - Computes a cache key (server + operation + normalized params)
   - Checks cache entry
   - If valid → returns cached response (**HIT**)
   - If missing/expired → calls upstream server (**MISS**)
4. Response is optionally cached with TTL
5. Request metadata and timings are logged
6. Response is returned to the client

---

## Caching Model

- **TTL-based caching** per upstream server
- Cache entries stored in SQLite (`cache_entries` table)
- Lazy eviction:
  - Expired entries are deleted when accessed
- Periodic garbage collection:
  - Background thread deletes expired entries every ~60 seconds
- Errors are **never cached**

---

## Latency Measurement

Two latency metrics are recorded:

- `latency_ms`  
  Total time spent inside the proxy-core  
  *(from request arrival to response ready)*

- `upstream_latency_ms`  
  Time spent waiting for the upstream MCP server  
  *(not including cache hits)*

> Client-side LLM latency (e.g. Claude reasoning time) is intentionally **not measured**,  
> as it is outside the proxy’s responsibility.

---

## SQLite Schema

### `cache_entries`
Stores active cache data:
- `cache_key`
- `expires_at`
- `response_json`

### `requests`
Append-only audit log containing:
- server_id, operation
- parameters
- cache status (HIT / MISS / NO_CACHE / ERROR)
- latency measurements
- response snapshot

---

## Configuration

The proxy is configured using a JSON file.

### Example: `proxy_core_config.example.json`

```json
{
  "db_path": "proxy_cache.sqlite",
  "listen": { "host": "0.0.0.0", "port": 8765 },

  "defaults": {
    "cache_ttl_s": 3600,
    "timeouts_s": {
      "initialize": 60,
      "tools/list": 60,
      "tools/call": 180
    }
  },

  "servers": {
    "wiki": {
      "cmd": ["npx", "-y", "wikipedia-mcp-server"],
      "cache_ttl_s": 86400
    },
    "open-meteo": {
      "cmd": ["npx", "-y", "open-meteo-mcp-server"],
      "cache_ttl_s": 600
    }
  }
}

