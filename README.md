# CORE v5.0 🧠
### Personal AGI Orchestration System

> Built by **REINVAGNAR** 🇮🇩 — Indonesia

---

## What is CORE?

CORE is a personal AGI system — a persistent, always-on brain that lives in the cloud, connects to Claude Desktop via MCP, and accepts tasks from anywhere via Telegram.

Currently at **Step 0** — MCP server fully operational, memory connected, bot live.

---

## Architecture

```
You (Claude Desktop / Telegram)
    ↓ MCP protocol / Telegram webhook
Railway — core.py (single process, port 8080)
    ├── FastAPI HTTP server
    ├── MCP server (/mcp/*)
    ├── Telegram bot (/webhook)
    └── Queue poller (30s)
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
| Supabase | Memory — knowledge, mistakes, playbook, state |
| GitHub | Source of truth — code + master prompt |
| Telegram | Remote control — @reinvagnarbot |
| Cloudflare | Credential vault — core-vault worker |

---

## MCP Tools (14)

Claude Desktop connects via `/mcp/startup` and gets full system context.

| Permission | Tools |
|-----------|-------|
| READ | `get_state`, `get_system_health`, `get_constitution`, `get_training_status`, `search_kb`, `get_mistakes`, `read_file`, `sb_query` |
| WRITE | `update_state`, `add_knowledge`, `log_mistake`, `notify_owner`, `sb_insert` |
| EXECUTE | `write_file` |

---

## Telegram Commands

```
/start   — system status overview
/status  — component health check
/tasks   — recent task queue
/ask X   — search knowledge base

Any other message → queued for execution (Step 3)
```

---

## Master Prompt

Lives in two places simultaneously:
- **Supabase** `master_prompt` table — live source of truth
- **GitHub** `master_prompt.md` — offline reference

Fetch latest:
```
https://raw.githubusercontent.com/pockiesaints7/core-agi/main/master_prompt.md
```

---

## Roadmap

| Step | Scope | Status |
|------|-------|--------|
| 0 | MCP server + Telegram bot + queue | 🔄 In progress |
| 1 | Claude Desktop fully connected | ⏳ Pending |
| 2 | Task execution via MCP tools | ⏳ Pending |
| 3 | 24/7 training loop + agent pipeline | ⏳ Pending |

---

## Owner

**REINVAGNAR** 🇮🇩  
Indonesia  
GitHub: [@pockiesaints7](https://github.com/pockiesaints7)  
Telegram Bot: [@reinvagnarbot](https://t.me/reinvagnarbot)
