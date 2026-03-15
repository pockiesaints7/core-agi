# CORE SESSION MASTER
> Last updated: 2026-03-16 | Owner: REINVAGNAR | Version: CORE v6.0
> This file is static — no longer auto-written by session_end.

## last_good_commit: 2026-03-15 (post SESSION.md write removal refactor)
> Railway recovery: use `github:get_file_contents` to read this file, restore via `github:push_files`.
> Do NOT use core-agi: tools when Railway is confirmed down — they all fail simultaneously.

---

## CLAUDE.AI / MOBILE BOOTSTRAP

When on claude.ai (no MCP), boot CORE manually:
1. `web_fetch https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md`
2. Use `sb_query` to load open tasks: `table=task_queue filters=source=in.(core_v6_registry,mcp_session)&status=in.(pending,in_progress)&order=priority.desc`
3. Use `POST /patch` for any source file edits (never gh_search_replace from web)
4. Use `github:*` tools for all other file reads/writes

---

## AUTONOMOUS MODE PROTOCOL

When user says "activate autonomous mode":
1. Launch daemon: `C:\Python314\python.exe "C:\Users\rnvgg\.claude-skills\selfchat\core_selfchat.py" --mode watch`
2. Write seed prompt to `C:\Users\rnvgg\.claude-skills\selfchat\prompt.txt`
3. Daemon sends prompt when Claude goes idle (polls for "Stop response" button absence)
   - Before every send: scroll to bottom (click 744,500 → End key → click 979,867) — Claude Desktop does NOT auto-scroll
4. Claude responds → **IMMEDIATELY write next prompt to prompt.txt** ← CRITICAL, loop dies without this
5. Repeat until task complete or user says stop
6. Stop: write `stop` to `status.txt`

---

## ACTIVE RULES

| Rule | Detail |
|---|---|
| `read_file` / `write_file` | OMIT `repo` arg — defaults to pockiesaints7/core-agi |
| `sb_query` | Use `filters` param, NOT `query_string` |
| Source file edits (.py) | Use `patch_file` — has py_compile guard. NEVER use `multi_patch` for .py files. |
| Source file edits (non-.py) | `gh_search_replace` (small) or `multi_patch` (batch) |
| Editing source from claude.ai | `POST /patch` ONLY |
| `processed_by_cold` | Use `eq.0` / `eq.1` (integer), NOT `eq.true` / `eq.false` |
| Structural change | Update operating_context.json, then KB |
| Task status | ALWAYS update task_queue via `task_update` tool before session_end. task_queue is source of truth. Raw `sb_patch` is fallback only. |
| Deploy pattern | `patch_file` → Railway auto-deploys → wait 35s → `build_status()`. Manual redeploy (no code): `redeploy()` → 35s → `build_status()`. `deploy_and_wait` is functional but not preferred — use build_status pattern instead. |
| Session end | Always call `session_end` — logs session + hot_reflection in one call |
| evolution_queue | Only `knowledge`, `code`, `config` change_types — never `backlog` |
| Railway recovery | last_good_commit above → restore via github: tools. Never retry core-agi: tools when Railway is down. |
| gh_search_replace on Unicode files | SKIP if file contains em-dashes or non-ASCII. Use github:get_file_contents + github:create_or_update_file directly. |
| SESSION.md | Static — never auto-written. Only edit manually when autonomous mode protocol changes or active rules change. |
