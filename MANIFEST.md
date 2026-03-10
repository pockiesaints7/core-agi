# CORE v5.0 Manifest
Updated: 2026-03-11
Step: 0 — MCP Server + Telegram Bot + Queue Poller

## Architecture
Single process, single port (8080), Groq LLM, Railway Hobby.

## Files
```
core.py                  — Main process (FastAPI + MCP + Telegram + Queue)
constitution.txt         — Immutable CORE constitution
resource_ceilings.json   — Hard rate limits (not evolvable)
requirements.txt         — Python dependencies
Procfile                 — Railway entry: python core.py
railway.json             — Railway deploy config
vault/worker.js          — Cloudflare credential vault (mirrors core.py env vars)
wrangler.toml            — Cloudflare Workers deploy config
```

## mcp_tools/ (available for future use, not active in Step 0)
```
mcp_tools/actions.py     — Context engine, session state
mcp_tools/brain.py       — CRUD brain ops
mcp_tools/brain_health.py — Health scanner
mcp_tools/changelog.py   — Audit trail
mcp_tools/db.py          — DB helpers
```

## Endpoints
```
GET  /             — Root health + system status
GET  /health       — Full component health (Supabase, Groq, Telegram, GitHub)
POST /webhook      — Telegram bot webhook
POST /mcp/startup  — Claude Desktop session start (3 auto-calls included)
POST /mcp/auth     — Auth only, returns session token
POST /mcp/tool     — Execute MCP tool
GET  /mcp/tools    — List all tools + permissions
```

## MCP Tools (14)
```
READ:    get_state, get_system_health, get_constitution, get_training_status
         search_kb, get_mistakes, read_file, sb_query
WRITE:   update_state, add_knowledge, log_mistake, notify_owner, sb_insert
EXECUTE: write_file
```

## Background Threads
```
queue_poller — checks task_queue every 30s (Step 0: acknowledges only)
```

## Not Active Yet (Step 3)
```
training_loop  — 24/7 Groq self-improvement
execute_task   — multi-agent task pipeline
ORCH/CRITIC/EVOLVER agents
```

## Env Vars (Railway)
```
Required: GROQ_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY
          TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_PAT, MCP_SECRET
Optional: GROQ_MODEL, GROQ_MODEL_FAST, GITHUB_USERNAME, PORT
Auto:     RAILWAY_PUBLIC_DOMAIN (injected by Railway)
```
