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
4. `SESSION.md` on GitHub — current task + step

This file is the lowest authority. When anything conflicts, live tools win.

---

## Session Protocol

**Desktop:** `session_start` → full boot context (health, counts, last session, mistakes, system_map).

**Web:** `get_state` → last session, counts, pending tasks. `get_system_health` → component status. Surface any unprocessed hot reflections or pending evolutions to the owner.

End every Desktop session with `session_end`. Never skip it.

---

## Immutable Rules

- Read before write. Always.
- Check `get_mistakes(domain=X)` before any write in that domain.
- Never query tombstone tables — check `operating_context.tombstone_tables` for the current list.
- Never expose credentials. Never disable rollback or Telegram notifications.
- When in doubt, do less and ask.

---

## What to Fetch Live (never hardcode here)

Tool list, KB count, active tables, tool parameters, current step, pending evolutions, system health, tombstone tables — all of these change as CORE evolves. Always fetch from `get_state` or `operating_context.json`.

If this file contradicts live tool output, the live tool wins. Flag the contradiction to the owner.
