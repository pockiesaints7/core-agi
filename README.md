# CORE v5.4 🧠
### Personal AGI Orchestration System

> Built by **REINVAGNAR** 🇮🇩 — Indonesia

---

## What is CORE?

CORE is a personal AGI system — a persistent, always-on brain that lives in the cloud, connects to Claude Desktop via MCP, and accepts tasks from anywhere via Telegram. It self-improves through a hot→cold training pipeline, manages its own knowledge base, and can modify its own codebase.

Currently at **v5.4** — fully operational with 50 MCP tools, autonomous training loop, and GOD MODE power tools.

---

## Architecture

```
You (Claude Desktop / Telegram)
    ↓ MCP protocol / Telegram webhook
Railway — core.py (FastAPI, port 8080)
    ├── MCP dispatcher (/mcp)
    ├── Telegram webhook (/telegram)
    ├── Queue poller (60s, notify-only)
    └── Background training loop
    ↓
Supabase — jarvis-brain (memory & knowledge)
    ↓
GitHub — pockiesaints7/core-agi (source of truth)
```

---

## Stack (100% Free Tier)

| Service | Role |
|---------|------|
| Railway | Host — single process, 24/7 |
| Groq | LLM — llama-3.3-70b (main), llama-3.1-8b (fast) |
| Supabase | Memory — knowledge, mistakes, reflections, evolutions |
| GitHub | Source of truth — code + state files |
| Telegram | Remote control — @reinvagnarbot |
| Cloudflare | Credential vault — core-vault worker |

---

## MCP Tools (50)

Claude Desktop connects via `/mcp` and gets full system context in one call (`session_start`).

| Permission | Count | Key Tools |
|-----------|-------|-----------|
| READ | 27 | `get_state`, `search_kb`, `get_mistakes`, `read_file`, `stats`, `build_status`, `core_py_fn` |
| WRITE | 15 | `add_knowledge`, `log_mistake`, `reflect`, `approve_evolution`, `sb_bulk_insert` |
| EXECUTE | 8 | `gh_search_replace`, `multi_patch`, `redeploy`, `deploy_and_wait`, `core_py_rollback` |

---

## Self-Improvement Pipeline

```
Claude session → hot_reflection → cold_processor → evolution_queue → approve → applied
```

CORE distills patterns from sessions, queues code/knowledge evolutions, and applies them with owner approval via Telegram.

---

## Telegram Commands

```
/start   — system status overview
/status  — component health check
/tasks   — recent task queue
/ask X   — search knowledge base

Any task → notify owner for approval (queue_poller, 60s)
```

---

## Live State

Always-current session state: [SESSION.md](./SESSION.md)

Raw fetch:
```
https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md
```

---

## Owner

**REINVAGNAR** 🇮🇩
Indonesia
GitHub: [@pockiesaints7](https://github.com/pockiesaints7)
Telegram Bot: [@reinvagnarbot](https://t.me/reinvagnarbot)
