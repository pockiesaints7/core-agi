# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 1 COMPLETE ✅ — Step 2 PENDING pre-task

---

## ⚠️ PENDING TASK — DO THIS BEFORE STEP 2

**Every new Claude session must offer this first before proceeding to Step 2.**

### Task: Fix `t_state()` in core.py — operating_context + sb_query param bug

**Why:** During 2026-03-11 session, full 14-tool audit revealed that fresh Claude sessions
fail on basic tool calls because they lack operating context (repo name, param conventions,
schema rules). After studying the codebase the tools started working — meaning the failures
were context failures, not code bugs. One real code bug was also found.

**What to fix (single push to core.py):**

1. **Add `operating_context` block to `t_state()`** — permanent, hardcoded, never stale:
   ```python
   "operating_context": {
       "repo": "pockiesaints7/core-agi",
       "tool_rules": {
           "read_file":  "OMIT repo arg — default is correct",
           "write_file": "OMIT repo arg — default is correct",
           "sb_query":   "param is 'filters' not 'query_string'",
           "log_mistake": "required: context, what_failed, fix. optional: domain, root_cause, how_to_avoid, severity",
           "sb_insert.sessions": "required: summary(str), actions(array), interface(str)"
       },
       "verify_rule": "Never report success without read-back confirmation",
       "mistakes_rule": "Before any remote write: call get_mistakes(domain=X) first"
   }
   ```

2. **Auto-fetch SESSION.md in `t_state()`** — for living state (current step, next action):
   ```python
   try:
       session_md = gh_read("SESSION.md")[:1500]
   except:
       session_md = "SESSION.md unavailable"
   # include as "session_md" key in return dict
   ```

3. **Fix `sb_query` MCP schema param** — `query_string` → `filters` (mismatch between
   MCP tool schema and actual function signature causes KeyError on every call with filters)

4. **Fix `t_state()` note field** — still says "Training starts Step 3", update to reflect
   current step accurately.

**Status:** NOT DONE — waiting for next session to implement.

---

## What was done (2026-03-11)

- ✅ Step 0+1 complete — core.py live, Claude Desktop connected via mcp-remote
- ✅ Full 14-tool audit: 8/14 passed initially, 14/14 after debug
- ✅ Fixed `t_log_mistake` — now sends root_cause, how_to_avoid, severity (schema match)
- ✅ Identified root causes: read_file/write_file failed due to wrong repo arg (not a bug)
- ✅ Identified sb_query MCP schema bug: exposes `query_string` but code uses `filters`
- ✅ Designed operating_context solution — split permanent vs living state
- ✅ Decision: `current_step`/`next_action`/`known_bugs` must NOT be hardcoded in t_state()
  because they go stale. Only permanent tool rules go in t_state(). SESSION.md handles living state.

## System state
- Railway: live at core-agi-production.up.railway.app
- MCP: Streamable HTTP, protocol 2024-11-05, 14 tools active
- Supabase: ok | Groq: ok | Telegram: ok | GitHub: ok
- Knowledge base: 330+ entries | Sessions: 180+ | Mistakes: 80+

## Active tables
knowledge_base, mistakes, sessions, task_queue,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency, changelog

## Dropped tables (do not query)
playbook, memory, master_prompt, patterns, projects, training_sessions,
training_sessions_v2, training_flags, session_learning, agent_registry,
knowledge_blocks, agi_mistakes, stack_registry, vault_logs, vault

## IMPORTANT for Claude Desktop sessions
- NEVER pass `repo` arg to read_file or write_file
- NEVER use `query_string` param for sb_query — use `filters`
- ALWAYS read-back after every write before reporting success
- ALWAYS call get_mistakes(domain=X) before any remote write
- Step 2 is next BUT offer pending task above first
