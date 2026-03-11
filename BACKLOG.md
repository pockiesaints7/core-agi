# CORE Improvement Backlog

_Auto-generated and maintained by `background_researcher()` running 24/7 on Railway._
_CORE simulates tasks every hour, identifies gaps, and writes here without owner intervention._

**Last updated:** Initializing...
**Status:** Waiting for first researcher cycle (runs ~60 min after Railway startup)

---

## How This Works

Every hour, CORE automatically:
1. Picks 3 random domains (code, business, legal, creative, medical, finance, data, academic)
2. Simulates sample tasks from each domain through the routing engine
3. Asks Groq: "What capabilities is CORE missing to handle these tasks better?"
4. Deduplicates, prioritizes, and writes results here
5. Persists items to `knowledge_base` (domain=backlog) for survivability across restarts
6. Sends Telegram notification if P4/P5 (high priority) items are found

## How To Act On Items

**Via Telegram:**
- `/backlog` — see pending items
- `/backlog 4` — see only P4+ (high priority)

**Via MCP (Claude Desktop):**
- `get_backlog(status="pending", min_priority=3)` — query backlog
- `backlog_update(title="...", status="in_progress")` — mark item
- `read_file("BACKLOG.md")` — read this file

**Workflow when you come online:**
1. `/backlog` on Telegram — see what CORE found
2. Pick items to implement
3. Implement (surgical patches via `/patch` endpoint)
4. `backlog_update(title="...", status="done")`
5. CORE keeps discovering more while you sleep

---

_Items will appear here after first researcher cycle. Railway deploys in ~2 min._
