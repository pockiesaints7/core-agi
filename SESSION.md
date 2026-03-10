# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 1 COMPLETE ✅

### What was done this session (2026-03-11)
- ✅ Step 0 already complete (core.py live on Railway, all health green)
- ✅ Diagnosed MCP connection failure: mcp-remote using http-first (Streamable HTTP), core.py only had SSE GET → 405 error
- ✅ Fixed core.py: added `POST /mcp/sse` Streamable HTTP transport + proper MCP JSON-RPC handler (initialize, tools/list, tools/call)
- ✅ Added `GET /mcp/sse` with correct SSE protocol (endpoint event first)
- ✅ Added `POST /mcp/messages` for SSE session routing
- ✅ Pushed fix to GitHub → Railway auto-deployed
- ✅ Claude Desktop connected: `Connected to remote server using StreamableHTTPClientTransport`
- ✅ tools/list returned all 14 tools
- ✅ Step 1 COMPLETE

### System state
- Railway: live at core-agi-production.up.railway.app
- MCP transport: Streamable HTTP (http-first strategy)
- MCP protocol version: 2024-11-05
- Tools: 14 active (get_state, get_system_health, get_constitution, get_training_status, search_kb, get_mistakes, read_file, sb_query, update_state, add_knowledge, log_mistake, notify_owner, sb_insert, write_file)
- Supabase: ok | Groq: ok | Telegram: ok | GitHub: ok
- Knowledge base: 329 entries
- Sessions: 177+
- Mistakes: 79

### Next step: Step 2 — Audit Training Logic
- Audit checklist: input/output clear, offline-safe, constitution check, rollback trigger, convergence rules, token budget
- Design hot_reflections → cold_reflections → evolution_queue pipeline
- Tables already exist in Supabase (empty, schema correct)

### IMPORTANT for Claude Desktop sessions
- Step 1 is COMPLETE. MCP is connected.
- Do NOT say Step 0 is pending or /mcp/startup needs testing — that is outdated.
- Current status: Step 2 is next.
- Always call get_state(), get_system_health(), get_constitution() at session start.
- Never hallucinate state — always call the actual MCP tools.
