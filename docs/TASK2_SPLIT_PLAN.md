# Task 2 — Architecture Split Execution Plan
> Created: 2026-03-13 | Status: READY TO EXECUTE
> Prereq: Task 1 ✅ complete

---

## Objective

Split `core.py` (3097 lines, ~157KB) into 4 focused modules.
**Goal:** maintainability, faster edits, clearer ownership per domain.

---

## Current File Map (verified 2026-03-13)

| Lines | Section | Target Module |
|---|---|---|
| 1–113 | Docstring + fix log | `core_main.py` (header) |
| 114–162 | Imports + constants + config | `core_main.py` (shared) |
| 163–231 | RateLimiter class + Supabase helpers (sb_get/post/patch/upsert) | `core_main.py` |
| 232–318 | Telegram helpers + GitHub helpers (gh_read/write, _gh_blob_*) | `core_github.py` |
| 319–411 | Step cache + Supabase helpers (get_latest_session, get_system_counts) + self-sync | `core_main.py` |
| 412–429 | MCP session management (_sessions, mcp_new, mcp_ok) | `core_main.py` |
| 430–819 | Training pipeline: auto_hot_reflection, run_cold_processor, apply_evolution, reject_evolution, cold_processor_loop | `core_train.py` |
| 820–2261 | All t_* MCP tool functions (t_state through t_logs) | `core_tools.py` |
| 2262–2430 | background_researcher + _extract_real_signal + _run_simulation_batch + bulk_apply | `core_train.py` |
| 2431–2708 | t_deploy_status, t_build_status, t_crash_report + TOOLS registry dict | `core_tools.py` + end of file |
| 2709–2743 | MCP JSON-RPC handler (handle_jsonrpc, _mcp_tool_schema) | `core_main.py` |
| 2744–3077 | FastAPI app, all routes, Pydantic models, Telegram bot handler, queue_poller | `core_main.py` |
| 3078–3097 | on_start() startup hook + uvicorn entry | `core_main.py` |

---

## Module Definitions

### `core_main.py` (entry point, ~900 lines)
- All imports
- All constants (GROQ_MODEL, SUPABASE_URL, etc.)
- RateLimiter class
- Supabase helpers (sb_get, sb_post, sb_patch, sb_upsert, sb_post_critical)
- Step cache + get_current_step()
- MCP session management (mcp_new, mcp_ok)
- MCP JSON-RPC handler (handle_jsonrpc)
- FastAPI app + all routes
- Telegram bot handler
- queue_poller
- on_start() startup
- Imports from: core_github, core_tools, core_train

### `core_github.py` (~200 lines)
- _ghh()
- gh_read(), gh_write()
- _gh_blob_read(), _gh_blob_write()
- notify() (Telegram)
- set_webhook()
- Depends on: constants from core_main (GITHUB_PAT, TELEGRAM_TOKEN, etc.)

### `core_tools.py` (~1800 lines)
- All 50 t_* functions
- TOOLS registry dict
- _mcp_tool_schema()
- Depends on: core_main (sb_*, L), core_github (gh_*, notify), core_train (run_cold_processor, apply_evolution)

### `core_train.py` (~700 lines)
- auto_hot_reflection()
- run_cold_processor()
- apply_evolution()
- reject_evolution()
- cold_processor_loop()
- background_researcher()
- _extract_real_signal()
- _run_simulation_batch()
- run_kb_mining() (if present)
- Depends on: core_main (sb_*, L), core_github (gh_*, notify)

---

## Shared State Problem

These globals are used across modules and must live in `core_main.py`:

```python
L = RateLimiter()          # used by sb_post, sb_post_critical, notify, gh_write
GITHUB_REPO                # used by gh_*, t_read_file, t_write_file
MCP_SECRET                 # used by mcp_new
GROQ_API_KEY / GROQ_MODEL  # used by groq_chat, run_cold_processor, background_researcher
SUPABASE_URL / SUPABASE_SVC / SUPABASE_ANON  # used by all sb_* functions
TELEGRAM_TOKEN / TELEGRAM_CHAT  # used by notify
_sessions                  # MCP session dict
_last_cold_run, _last_cold_kb_count, _last_research_run  # training loop globals
_startup_times             # crash detection
```

**Solution:** Keep all env vars + globals in `core_main.py`. Other modules import what they need.

---

## Import Chain (no circular deps)

```
core_main.py
    ├── imports core_github  (gh_*, notify, set_webhook)
    ├── imports core_train   (run_cold_processor, apply_evolution, cold_processor_loop, background_researcher)
    └── imports core_tools   (TOOLS, handle_jsonrpc)

core_github.py
    └── imports core_main    (L, GITHUB_PAT, TELEGRAM_TOKEN, TELEGRAM_CHAT, GITHUB_REPO)

core_tools.py
    ├── imports core_main    (sb_*, get_system_counts, get_latest_session, L, constants)
    ├── imports core_github  (gh_read, gh_write, notify)
    └── imports core_train   (run_cold_processor, apply_evolution, reject_evolution)

core_train.py
    ├── imports core_main    (sb_*, L, constants, globals)
    └── imports core_github  (gh_write, notify)
```

⚠️ **Circular import risk:** core_github needs `L` from core_main, and core_main imports core_github.
**Fix:** Move `L = RateLimiter()` instantiation AFTER imports in core_main, OR use a shared `core_config.py`
with only constants + RateLimiter — no app-level imports. Recommend `core_config.py` approach.

---

## Revised Module Plan (with core_config.py)

```
core_config.py    — constants, env vars, RateLimiter instance (no imports from other modules)
core_github.py    — gh_*, notify, set_webhook  (imports core_config)
core_train.py     — training pipeline          (imports core_config, core_github)
core_tools.py     — all t_*, TOOLS dict        (imports core_config, core_github, core_train)
core_main.py      — FastAPI, routes, MCP, startup (imports all above)
```

Railway entry point stays `core_main.py`. Update `Procfile` if present.

---

## Execution Steps (Desktop session)

1. **Read** current core.py fully into context via gh_read_lines (already done 2026-03-13)
2. **Create core_config.py** — extract lines 114–191 (imports + constants + RateLimiter)
3. **Create core_github.py** — extract lines 232–318 + notify (lines 234–255) + update imports
4. **Create core_train.py** — extract lines 430–819 + 2262–2430 + update imports
5. **Create core_tools.py** — extract lines 820–2261 + 2431–2708 + TOOLS dict + update imports
6. **Rewrite core_main.py** — keep routes + MCP handler + startup, import from above 4 modules
7. **Test:** `redeploy → verify_live` → smoke test all 50 tools
8. **Rename** core.py → core_legacy.py once smoke test passes
9. **Update** operating_context.json: entry_point core.py → core_main.py

---

## Rollback Plan

- Keep `core_legacy.py` in repo until Task 3 is complete and stable
- If split breaks Railway: rename core_legacy.py → core.py, delete new modules, redeploy
- Last known good SHA: verify with `t_build_status` before starting

---

## Estimated Tool Calls

~25–35 tool calls for full split + smoke test. Plan for 2 Desktop sessions.
Session A: Create core_config.py, core_github.py, core_train.py (15 calls)
Session B: Create core_tools.py, core_main.py rewrite, smoke test (20 calls)

---

## Status

- [x] Line map verified (2026-03-13)
- [x] Module plan finalized (2026-03-13)
- [x] Circular import risk identified + solution documented
- [ ] Session A: Create core_config.py + core_github.py + core_train.py
- [ ] Session B: Create core_tools.py + rewrite core_main.py + smoke test
- [ ] Update SESSION.md Task 2 sub-steps with results
