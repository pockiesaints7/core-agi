# CORE SESSION MASTER
> Last updated: 2026-03-16 | Owner: REINVAGNAR | Version: CORE v6.0
> This file is static — no longer auto-written by session_end.

## last_good_commit: 2026-03-15 (post SESSION.md write removal refactor)
> Railway recovery: use `github:get_file_contents` to read this file, restore via `github:push_files`.
> Do NOT use core-agi: tools when Railway is confirmed down — they all fail simultaneously.

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
