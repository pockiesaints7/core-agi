# CORE

Personal AGI Orchestration System — built by REINVAGNAR, Indonesia.

CORE is a persistent, self-improving AI brain running on Railway, Supabase, and GitHub. It connects to Claude via MCP, learns from every session, and evolves itself over time. Not a chatbot wrapper. An operating system for AI-assisted work.

---

## What it does

Every session, CORE captures patterns from the work done. A cold processor distills those patterns, queues proposed changes, and waits for owner approval. Once approved, the changes are applied — and CORE is measurably smarter than before. The loop runs indefinitely.

---

## Architecture

Claude connects to a FastAPI server on Railway via MCP. That server is the brain — it dispatches tool calls, handles Telegram webhooks, and runs a background queue poller every 60 seconds. State and source of truth live in two places: Supabase holds the memory (knowledge base, mistakes, evolutions, sessions), and GitHub holds the code and state files.

---

## MCP Surface

50 tools across three classes:

- Read (27) — query state, search the knowledge base, inspect code, check builds
- Write (15) — log knowledge, reflect, queue evolutions, send notifications  
- Execute (8) — patch code, redeploy, rollback to last good commit

One call to `session_start` bootstraps full context. One call to `session_end` closes the loop and logs the session.

---

## Current state

Live session state is always at SESSION.md in this repo. The knowledge base currently holds 3,368 entries across 205 sessions. Training pipeline is active.

---

REINVAGNAR · Indonesia · github.com/pockiesaints7
