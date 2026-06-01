# MCP Smart Proxy

A lightweight and extensible **Model Context Protocol (MCP) proxy** that sits between LLM clients (e.g. Claude Desktop) and multiple upstream MCP servers.

The proxy provides:

* Transparent MCP routing
* Configurable caching (exact and semantic)
* Latency measurement
* Request auditing
* Per-server cache policies

The system is designed to be **generic**, **extensible**, and **server-agnostic**, allowing multiple MCP servers (e.g. Wikipedia, weather APIs, utility tools) to be integrated without server-specific proxy logic.

---

# Architecture Overview

The system consists of two main components.

## Proxy Core (`proxy_core.py`)

The central TCP server responsible for:

* Accepting MCP requests
* Managing cache logic
* Forwarding requests to upstream MCP servers
* Measuring latency
* Logging requests and responses

The proxy core acts as the orchestration layer of the system.

---

## Proxy Endpoint (`proxy_endpoint.py`)

A lightweight client-side bridge that:

* Connects an MCP-compatible client to the proxy core
* Forwards MCP JSON-RPC messages
* Identifies the upstream server (`server_id`)
* Provides a `caller_id` for request tracing

The endpoint remains intentionally thin and transparent.

---

# Request Flow

1. Client sends request → proxy endpoint
2. Endpoint forwards request → proxy core
3. Proxy core:

* Computes a normalized cache key
* Applies configured cache policy
* Checks cache layers
* Returns cached response on HIT
* Otherwise forwards request upstream

4. Response is optionally cached
5. Request metadata and latency are logged
6. Response is returned to the client

---

# Caching Model

The proxy supports a two-layer caching architecture.

## Exact Cache (SQLite)

The exact cache stores deterministic request-response mappings using normalized parameters.

Stored data includes:

* cache key
* expiration timestamp
* response payload

This layer provides precise cache hits.

---

## Semantic Cache (Qdrant)

The semantic cache supports approximate matching using embeddings.

This enables:

* semantically similar queries
* paraphrase handling
* reduced redundant upstream calls

Semantic caching is optional and configurable.

---

# TTL and Cache Cleanup

Both cache layers use **TTL-based expiration**.

The system performs:

* startup cleanup of expired entries
* periodic garbage collection
* preservation of valid cache entries

Restarting the proxy does **not** clear cache automatically.

Errors are never cached.

---

# Per-Server Cache Policies

Caching behavior is configurable per upstream server.

Supported configuration options include:

```json
{
  "cache_enabled": true,
  "semantic_cache_enabled": true,
  "cache_ttl_s": 3600
}
```

This allows:

* full caching
* exact-only caching
* no caching

Example use cases:

* Weather APIs → shorter TTL
* Wikipedia → longer TTL
* Time services → caching disabled

---

# Latency Measurement

The proxy records two latency metrics.

## `latency_ms`

Total proxy processing time.

Includes:

* cache handling
* upstream interaction
* response preparation

---

## `upstream_latency_ms`

Time spent waiting for the upstream MCP server.

Cache hits do not contribute to this metric.

LLM reasoning latency (e.g. Claude generation time) is intentionally excluded.

---

# Request Logging

SQLite maintains an append-only request log containing:

* server ID
* operation
* parameters
* cache status
* latency measurements
* response snapshot

This supports observability and offline analysis.

---

# Configuration

The proxy is configured using a JSON configuration file.

Example:

```json
{
  "db_path": "proxy_cache.sqlite",
  "listen": {
    "host": "0.0.0.0",
    "port": 8765
  },

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
      "cache_enabled": true,
      "semantic_cache_enabled": true,
      "cache_ttl_s": 86400
    },

    "open-meteo": {
      "cmd": ["npx", "-y", "open-meteo-mcp-server"],
      "cache_enabled": true,
      "semantic_cache_enabled": true,
      "cache_ttl_s": 900
    },

    "time": {
      "cmd": ["uvx", "mcp-server-time"],
      "cache_enabled": false
    }
  }
}
```

---

# Running the Proxy

Start the proxy core:

```bash
python3 proxy_core.py --config proxy_core_config.json
```

Start an endpoint:

```bash
python3 proxy_endpoint.py \
  --host 127.0.0.1 \
  --port 8765 \
  --server-id wiki \
  --caller-id local-client
```

---

# Reliability

The proxy manages locally spawned MCP subprocesses and can automatically restart upstream processes if they terminate unexpectedly.

---

# Project Goals

The MCP Smart Proxy explores how intermediary proxy systems can:

* reduce redundant MCP traffic
* improve responsiveness
* support intelligent caching
* provide observability
* remain transparent to clients and upstream servers
