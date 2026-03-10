# CORE AGI Manifest
Updated: 2026-03-10 23:12 UTC

## Architecture
Single process, single port (8080), Groq only LLM.

## Files
- core.py              - CORE v5.0 main (bot + MCP + training loop + queue poller)
- constitution.txt     - Immutable CORE constitution
- resource_ceilings.json - Hard limits (not evolvable by CORE)
- requirements.txt     - Python dependencies
- Procfile             - Railway entry point

## mcp_tools/ (salvaged from jarvis-os, available for import)
- mcp_tools/actions.py       - Context engine, session state, boot, brain_write
- mcp_tools/brain.py         - CRUD brain ops
- mcp_tools/brain_health.py  - 13-check health scanner
- mcp_tools/changelog.py     - Audit trail system
- mcp_tools/db.py            - Dollar-quoting helpers, async SQL

## Endpoints
- GET  /            - Health + system status
- GET  /health      - Full component health check
- POST /webhook     - Telegram bot webhook
- POST /mcp/startup - Claude Desktop session start (3 auto-calls)
- POST /mcp/auth    - MCP auth only
- POST /mcp/tool    - Execute MCP tool
- GET  /mcp/tools   - List available tools

## MCP Tools (14)
READ:    get_state, get_system_health, get_constitution, get_training_status,
         search_kb, get_mistakes, read_file, sb_query
WRITE:   update_state, add_knowledge, log_mistake, notify_owner, sb_insert
EXECUTE: write_file

## Background threads
- training_loop  - Groq 24/7 self-improvement (every 45s)
- queue_poller   - task_queue checker (every 30s)