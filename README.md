# CORE AGI 🧠
### Universal Self-Improving AI Orchestrator

> Built by **REINVAGNAR** 🇮🇩 — Indonesia

---

## What is CORE?

CORE is a self-improving AGI orchestration system that gets smarter after every single task.

You give it one line. It figures out everything else.

```
"Make a website like Netflix but better"     → researches, designs, codes, deploys
"Project plan for Tier IV data center"       → researches standards, plans, documents
"Cost estimate for 47 floor building"        → researches rates, calculates, reports
```

---

## Architecture

```
You (anywhere)
    ↓ Telegram / Claude.ai / task_queue
Railway CORE orchestrator (24/7 online)
    ↓ calls Claude API with full context
Supabase jarvis-brain (memory & knowledge)
    ↓ 600+ knowledge entries injected per task
Claude Agents (researcher, engineer, designer, writer, analyst, qa)
    ↓ critic scores output, retries if <85
Results stored back → system gets smarter
Master prompt evolves → GitHub synced
```

---

## Self-Improving Loop

```
Task arrives
    → reads 600+ knowledge entries from jarvis-brain
    → reads 154 proven playbook methods
    → reads 72 mistakes to avoid
    → executes specialist agents
    → critic scores 0-100
    → stores new knowledge back
    → master prompt evolves if needed
    → GitHub synced (this file updates automatically)
```

Every run makes the next run better. Forever.

---

## Stack (100% Free Tier)

| Service | Role |
|---------|------|
| Claude API | Brain — all agent calls |
| Supabase | Memory — 600+ knowledge entries |
| Railway | Host — 24/7 orchestrator |
| GitHub | Code + offline master prompt sync |
| Telegram | Universal remote control |
| Vercel | Frontend |
| Google Drive/Sheets/Gmail | Files & data |

---

## Use From Anywhere

**Phone (Telegram):**
```
Open @reinvagnarbot
Send any task → executes fully automatically
/status  — system health
/prompt  — current master prompt version
/tasks   — recent tasks
/ask X   — search knowledge base
```

**Claude.ai / Claude Desktop:**
```
Preference prompt fetches master_prompt.md from this repo
Full system context loaded every session
You design → Railway executes → results stored
```

---

## Master Prompt

The master prompt lives in two places simultaneously:
- **Supabase** `master_prompt` table (source of truth, self-evolves)
- **GitHub** `master_prompt.md` (this repo, offline reference)

Both stay in sync automatically after every task.

Fetch latest:
```
https://raw.githubusercontent.com/pockiesaints7/core-agi/main/master_prompt.md
```

---

## Database (Supabase jarvis-brain)

| Table | Rows | Purpose |
|-------|------|---------|
| knowledge_base | 326+ | Domain knowledge |
| playbook | 154+ | Proven methods |
| mistakes | 72+ | Errors to avoid |
| memory | 121+ | System facts |
| patterns | growing | Task patterns |
| master_prompt | versioned | Self-evolving prompt |
| task_queue | - | Async job queue |
| session_learning | growing | Per-session learnings |

---

## Owner

**REINVAGNAR** 🇮🇩  
Indonesia  
GitHub: [@pockiesaints7](https://github.com/pockiesaints7)  
Telegram Bot: [@reinvagnarbot](https://t.me/reinvagnarbot)

---

*The future belongs to those who tinker with software like this. — Greg Isenberg*

*REINVAGNAR is tinkering from Indonesia. 🇮🇩*