# Internal Agent (DB-Only Search)

The Internal Agent is a standalone chat profile that provides database-only search — no web research, no MCP tools, no external APIs. It runs `ProcurementAgent` in `internal_only` mode.

## Purpose

The Internal Agent is designed for scenarios where:

- Staff need to search the internal product/supplier database quickly
- No internet access is available or desired
- A lightweight, focused agent is preferred over the full multi-agent graph

## Available Tools

| Tool | Description |
|---|---|
| Product search | Search by part number, description, or brand |
| Supplier search | Search by name, category, or location |
| Purchase history | Look up past transactions by product or supplier |
| RFQ management | Create and view RFQs (no supplier sourcing or web search) |

## What's Excluded

- ❌ Google Search grounding
- ❌ MCP tools
- ❌ Web research
- ❌ Supplier URL verification
- ❌ External API calls

## Architecture

The Internal Agent is a **single-node LangGraph graph** (no Supervisor routing):

```
User → ProcurementAgent (internal_only=True) → Response
```

It's compiled in `includes/graph.py` as `internal_graph` and exposed as the "Internal Agent" chat profile in `app.py`.

## How It Differs from Eagle Agent

| | Eagle Agent | Internal Agent |
|---|---|---|
| **Graph** | Multi-agent (Supervisor → 3 agents) | Single-agent |
| **Tools** | Product + web + MCP + RFQ | Product + RFQ only |
| **Research** | Google Search grounding | None |
| **MCP** | Dynamic tool loading | None |
| **Latency** | Higher (supervisor routing) | Lower (direct) |
