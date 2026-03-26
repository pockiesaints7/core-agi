# CORE SESSION MASTER
> Last updated: 2026-03-26 | Owner: REINVAGNAR | Version: CORE v6.0
> This file is static — no longer auto-written by session_end.

## Runtime: Oracle VM (NOT Railway)
CORE runs on Oracle Cloud Ubuntu VM via systemd service `core-agi`.
- SSH: `ubuntu@core-agi.duckdns.org` — key at `C:\Users\rnvgg\.claude-skills\ssh-key-2026-03-22.key`
- .env: `/home/ubuntu/core-agi/.env`
- MCP endpoint: `https://core-agi.duckdns.org/mcp/sse`

## Recovery
CORRECT RECOVERY after crash:
1. SSH to VM: check `systemctl status core-agi` and `journalctl -u core-agi -n 50`
2. If code broken: `cd /home/ubuntu/core-agi && git log --oneline -5` to find last good commit
3. Rollback: `git checkout <SHA> -- <file>` then `systemctl restart core-agi`
4. Deploy new code: push to GitHub → GitHub Actions calls `/deploy-webhook` → auto git pull + restart
5. PAT location: `C:\Users\rnvgg\.claude-skills\services\CREDENTIALS.md`

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
