# CORE AGI — Manifest
Updated: 2026-03-10 15:36:55 UTC

## Root Files
| File | Purpose |
|---|---|
| `orchestrator.py` | Railway bot + task executor + launches MCP server thread |
| `mcp_server.py` | MCP server port 8081 (CORE v5 Step 0) |
| `constitution.txt` | Immutable CORE constitution — never modified by any tool |
| `resource_ceilings.json` | Hard resource limits — only REINVAGNAR may change |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway entry: web=orchestrator.py (mcp_server runs as thread) |
| `MANIFEST.md` | This file — repo map, always current |
| `master_prompt.md` | CORE identity prompt (synced from Supabase) |
| `README.md` | Project overview |
| `.gitignore` | Git ignore rules |

## mcp_tools/ — MCP Tool Implementations
| File | Purpose |
|---|---|
| `mcp_tools/__init__.py` | Python package marker |
| `mcp_tools/db.py` | Async SQL client + dollar-quoting helpers (dq, esc) |
| `mcp_tools/actions.py` | Context engine, session state, boot, brain_write, session_end |
| `mcp_tools/brain.py` | CRUD brain ops (knowledge, mistakes, playbook, sessions) |
| `mcp_tools/brain_health.py` | 13-check health scanner — maintenance + growth flags |
| `mcp_tools/changelog.py` | Append-only audit trail for all system changes |

## Architecture
```
Claude Desktop
    ↓ MCP protocol
Railway (port 8080 public)
    ├── orchestrator.py  — Telegram bot + task executor + queue poller
    └── mcp_server.py    — MCP server (thread, port 8081 internal)
            ↓
    ├── Supabase jarvis-brain  — all persistent state
    ├── GitHub core-agi        — source of truth
    └── Telegram @reinvagnarbot — owner notifications
```

## Fix Log
| # | Fix | Status |
|---|---|---|
| 1 | Procfile single process + MCP thread launcher | ✅ done |
| 2 | Wire mcp_tools/ into mcp_server.py | ✅ done |
| 3 | MANIFEST.md updated | ✅ done |
| 4 | claude_desktop_config.json — register MCP | pending |
| 5 | model string update (claude-sonnet-4-5 → 4-6) | pending |