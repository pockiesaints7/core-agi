# CORE

Personal AGI Orchestration System — built by REINVAGNAR, Indonesia.

CORE is a persistent, self-improving AI brain. It connects to Claude Desktop via MCP, learns from every session, and evolves its own behavior over time. Not a chatbot wrapper. An operating system for AI-assisted work.

---

## What it does

Every session, CORE captures patterns from the work done. A cold processor distills those patterns into proposed behavioral changes, queued for owner approval. Once approved, they are applied — and CORE is measurably smarter than before. The loop runs indefinitely.

---

## Architecture

Five layers, all simultaneous:

| Layer | System | Role |
|---|---|---|
| Brain | Supabase | All persistent memory — KB, mistakes, evolutions, sessions |
| Executor | Oracle VM (`core-agi.service`) | Always-on FastAPI + MCP server, Telegram bot, cold processor |
| Skeleton | GitHub (`pockiesaints7/core-agi`) | Source of truth for all code and docs |
| Interface | Claude Desktop + Groq + Telegram | Reasoning, learning, owner interaction |
| Local PC | REINVAGNAR's Windows PC | Credentials, local execution, Desktop Commander |

MCP endpoint: `https://core-agi.duckdns.org/mcp/sse`

---

## MCP Surface

175+ tools across three classes:

- **Read** — query state, search the knowledge base, inspect code, get crypto prices
- **Write** — log knowledge, reflect, queue evolutions, send notifications
- **Execute** — patch code, redeploy, rollback, run scripts on VM

One call to `session_start` bootstraps full context. One call to `session_end` closes the loop and logs the session.

---

## Current state

Knowledge base: **6,081 entries** · Sessions: **962** · Mistakes: **1,248** · Evolutions applied: **1,314**
Training pipeline: active · Quality 7d avg: **0.786** (improving)

---

REINVAGNAR · Indonesia · github.com/pockiesaints7
