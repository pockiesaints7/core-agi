<![CDATA[<div align="center">

```
 ██████╗ ██████╗ ██████╗ ███████╗
██╔════╝██╔═══██╗██╔══██╗██╔════╝
██║     ██║   ██║██████╔╝█████╗  
██║     ██║   ██║██╔══██╗██╔══╝  
╚██████╗╚██████╔╝██║  ██║███████╗
 ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝
```

**Personal AGI Orchestration System**

`v5.4` · `live` · `self-improving` · `50 MCP tools`

*Built by **REINVAGNAR** 🇮🇩 Indonesia*

</div>

---

CORE is a persistent, cloud-native AGI brain. It connects to Claude Desktop via MCP, learns from every session, writes its own evolutions, and gets smarter over time — autonomously.

This is not a chatbot wrapper. It's an operating system for AI-assisted work.

---

## How it thinks

```
session
  └─ reflect          → captures patterns from the session
       └─ cold_processor   → distills patterns, queues evolutions
            └─ approve          → owner reviews via MCP
                 └─ applied          → CORE is now smarter
```

Every session feeds the loop. The system evolves itself.

---

## Architecture

```
Claude Desktop  ──MCP──▶  Railway (core.py)
                               ├── /mcp        tool dispatcher
                               ├── /telegram   webhook
                               └── queue_poller (60s, notify-only)
                                       │
                              ┌────────┴────────┐
                           Supabase          GitHub
                        (memory, KB,      (source of truth,
                         mistakes,         state files,
                         evolutions)       self-edits)
```

---

## MCP Surface (50 tools)

| Class | Count | What it does |
|---|---|---|
| READ | 27 | query state, search KB, inspect code, check builds |
| WRITE | 15 | learn, reflect, evolve, notify |
| EXECUTE | 8 | patch code, redeploy, rollback |

One call to `session_start` bootstraps the full context. One call to `session_end` closes the loop.

---

## Live state

→ [`SESSION.md`](./SESSION.md) — always current

```
https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md
```

---

<div align="center">

**REINVAGNAR** · 🇮🇩 Indonesia · [@pockiesaints7](https://github.com/pockiesaints7)

</div>
]]>