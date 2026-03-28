---
name: core-agi
description: a personal AGI orchestration system for REINVAGNAR. Use this skill whenever the user refers to CORE, their AGI system, Railway deployment, Supabase memory, training pipeline, evolutions, knowledge base, sessions, or mistakes.
---

## Interface Detection (do this first, silently)

- **Claude Desktop** → MCP tools respond. Call `session_start` immediately. It bootstraps full context in one call.
- **claude.ai web/mobile** → Call `get_state` + `get_system_health` before responding. Live state is always authoritative over this file.

Never rely on this file for counts, tool lists, schemas, or current system status. Always fetch live.

---

## Stable Addresses

| System | Address |
|---|---|
| Railway | https://core-agi-production.up.railway.app |
| GitHub repo | pockiesaints7/core-agi |
| Supabase project | qbfaplqiakwjvrtwpbmr |
| Telegram bot | @reinvagnarbot |
| Owner | REINVAGNAR |

---

## Source of Truth Hierarchy

1. Live MCP tools (`get_state`, `get_system_health`, `get_training_status`) — always freshest
2. `CORE_SELF.md` on GitHub — master structural doc
3. `operating_context.json` on GitHub — full DB schema + tool rules
4. `SESSION.md` on GitHub — SOP, rules, incident log (NOT task registry — that moved to Supabase)

This file is the lowest authority. When anything conflicts, live tools win.

---

## Session Protocol

**Desktop:**
1. `session_start` → full boot context (health, counts, last session, mistakes, system_map)
2. Query open tasks: `sb_query(table="task_queue", filters="source=eq.core_v6_registry&status=eq.pending&order=priority.desc")`
3. Pick highest priority non-blocked task — that is what to work on this session

**Web:** `get_state` → last session, counts. `get_system_health` → component status. Then query task_queue same as above.

End every Desktop session with `session_end`. Never skip it.

---

## SESSION.md — What It Is

SESSION.md is the **static operating manual** — SOP, rules, autonomous mode protocol, incident log, slim task index.

It is NOT the task registry. Tasks live in Supabase `task_queue` (source=core_v6_registry).
Never write task detail or tick checkboxes in SESSION.md.

---

## Task Lifecycle

**New task** → `sb_insert` into task_queue with source=core_v6_registry. Add one-liner to SESSION.md task index only.

**Subtask done** → note in `session_end` actions field. session_end summary is CORE's memory of progress.

**Full task done** → `update_state(key="task_done", value="TASK-N", reason="...")` + note in session_end.

**Every session** → session_end must list: which subtasks completed, what the next task/subtask is (new_step field).

---

## Immutable Rules

- Read before write. Always.
- Check `get_mistakes(domain=X)` before any write in that domain.
- Never query tombstone tables — check `operating_context.tombstone_tables` for the current list.
- Never use `multi_patch` on `.py` files — use `patch_file` instead (has py_compile guard).
- Never expose credentials. Never disable rollback or Telegram notifications.
- When in doubt, do less and ask.

---

## What to Fetch Live (never hardcode here)

Tool list, KB count, active tables, tool parameters, open tasks, pending evolutions, system health, tombstone tables — all change as CORE evolves. Always fetch from live tools or `operating_context.json`.

If this file contradicts live tool output, the live tool wins. Flag the contradiction to the owner.
