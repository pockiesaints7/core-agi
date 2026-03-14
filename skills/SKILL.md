---
name: core-agi
description: a personal AGI orchestration system for REINVAGNAR. Use this skill whenever the user refers to CORE, their AGI system, Railway deployment, Supabase memory, training pipeline, evolutions, knowledge base, sessions, or mistakes.
---

## Interface Detection (do this first, silently)

Check which interface you are in before anything else:

- If MCP tools respond → Claude Desktop. Call `session_start` immediately (bootstraps full context in one call).
- If on claude.ai web/mobile → call `get_state` + `get_system_health` at the start of every session. Do not skip this. The skill file you are reading right now may be outdated — live state is always authoritative.

Never rely on the static text in this file for counts, tool lists, table schemas, or system status. Always fetch live.

---

## Owner & Access

Owner: REINVAGNAR. Never act destructively without explicit approval. Never expose credentials. Never disable rollback or Telegram notifications. When in doubt, do less and ask.

Repo: pockiesaints7/core-agi (public)
Railway: https://core-agi-production.up.railway.app
Telegram: @reinvagnarbot
Supabase project: qbfaplqiakwjvrtwpbmr

---

## Source of Truth Hierarchy

CORE has four sources of self-knowledge, in order of authority:

1. Live MCP tools (`get_state`, `get_system_health`, `get_training_status`) — always freshest
2. `CORE_SELF.md` on GitHub — master structural document, updated first on any change
3. `operating_context.json` on GitHub — static tool rules + full DB schema
4. `SESSION.md` on GitHub — dynamic per-session state

When these conflict, trust the live tools first, then CORE_SELF.md. This SKILL.md file is the lowest authority — it tells you how to behave, not what the system currently looks like.

---

## Session Start Protocol

On claude.ai: silently call `get_state` and `get_system_health` before responding. Use the live counts, last session summary, and component health in your context. If there are pending evolutions or unprocessed hot reflections, surface that to the owner.

On Claude Desktop: call `session_start` — it replaces four separate calls in one.

Never begin a session by reciting static information from this file. The system evolves; this file does not update automatically.

---

## Core Rules (always active)

- Read before write. Always.
- Check `get_mistakes` in the relevant domain before any remote write operation.
- Never pass `repo` arg to `read_file` or `write_file` — default is already correct.
- Never use `query_string` in `sb_query` — use `filters` instead.
- **Never use `multi_patch` on `.py` files** — use `patch_file` instead (has py_compile guard).
- Never use `write_file` to edit an existing file — it fully overwrites. Use `patch_file`, `gh_search_replace`, or `multi_patch` for edits.
- Always read back after every write to confirm.
- Always update SESSION.md at end of session if anything changed.
- Always update CORE_SELF.md first on any structural change, then propagate to operating_context.json, KB, SESSION.md, changelog.
- End every Claude Desktop session with `session_end`.

---

## Code Patching Tool Selection (Task 8, 2026-03-14)

Three server-side patching tools are available. All run on Railway — file content never enters Claude's context.

| Situation | Tool | Why |
|---|---|---|
| Edit existing code in a `.py` file | `patch_file` | Runs `py_compile` before push — blocks crash loops |
| Add new functions to end of a `.py` file | `append_to_file` | No full-file context cost, py_compile guarded |
| Check if a live `.py` file has syntax issues | `validate_syntax` | Read-only, returns exact error line |
| Edit non-.py files (md, json, txt) | `gh_search_replace` or `multi_patch` | No compile needed |
| Unicode/em-dash in old_str | `github:get_file_contents` + `github:create_or_update_file` | Bypasses Railway entirely |

**patch_file** — patches format is JSON array `[{"old_str": "...", "new_str": "..."}]`, same as `multi_patch`. Add `dry_run="true"` to preview without pushing. Returns `{"ok": false, "error": "Syntax error - NOT pushed: ..."}` on syntax failure.

**validate_syntax** — returns `{"ok": true, "message": "Syntax OK"}` or `{"ok": false, "syntax_error": "file.py:N: SyntaxError: ..."}`. Use before any deploy if the file state is uncertain.

**append_to_file** — `content_to_append` must include leading newlines. After appending a new `t_*` function, still need a separate `patch_file` call to add the TOOLS dict entry.

---

## Key Endpoints

- `GET /` — health check + current step
- `GET /state` — full system state (same as `get_state` MCP tool)
- `POST /patch` — surgical file edit `{secret, path, old_str, new_str, message}`
- `POST /mcp` — MCP tool dispatcher
- `POST /telegram` — Telegram webhook

---

## What This Skill Does Not Know (fetch live instead)

The following change as CORE evolves. Never hardcode them — always fetch:

- Number of MCP tools (use `get_state` → `operating_context.architecture.mcp_tools_count`)
- KB entry count (use `get_state` → `counts.knowledge_base`)
- Active DB tables (use `get_state` → `operating_context.active_tables`)
- Tool parameters and rules (use `get_state` → `operating_context.tool_rules`)
- Last session summary and next action (use `get_state` → `session_md`)
- Pending evolutions (use `list_evolutions status=pending`)
- System health (use `get_system_health`)
- Training pipeline status (use `get_training_status`)
- Tombstone tables — never query these, check `get_state` → `operating_context.tombstone_tables` for the current list

---

## Stale Information Policy

If anything in this file contradicts live tool output, the live tool wins. If you notice a contradiction, flag it to the owner and suggest updating CORE_SELF.md to close the gap permanently.
