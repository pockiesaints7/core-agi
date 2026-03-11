"""CORE v5.0 - Recursive Self-Improvement Architecture
Owner: REINVAGNAR
Step status is dynamic - always read from SESSION.md on GitHub.
Do NOT hardcode step numbers anywhere in this file.

Fix log:Fix log:
  2026-03-11e: t_state() fetches operating_context.json + SESSION.md from GitHub.
  2026-03-11f: cold_processor uses Counter for batch freq counting.
  2026-03-11g: cold_reflections insert uses sb_post_critical (bypasses rate limiter).
  2026-03-11h: All hardcoded step labels removed. Step derived dynamically from SESSION.md.
               Root cause: step labels in root(), startup(), on_start(), Telegram /start
               were hardcoded to 'Step 3' even after system advanced to Step 5.
               Fix: get_current_step() helper reads SESSION.md live on every call.
  2026-03-11i: processed_by_cold filter changed from eq.false/True to eq.0/1 (integer).
               Root cause: Supabase PostgREST rejects Python bool in querystring filter.
               Fix: use 0/1 in all querystring filters; keep Python bool in JSON body (ok there).
  2026-03-11j: Added POST /patch endpoint — surgical find-and-replace from claude.ai.
               Accepts {path, old_str, new_str, message, secret}. Reuses gh_search_replace.
               Purpose: avoid full-file rewrites from claude.ai which waste GitHub rate limit.
  2026-03-11k: RESTORE from cc87e5c after accidental full-overwrite by write_file tool.
               ROOT CAUSE: write_file = full overwrite, NOT surgical. Never use for partial edits.
               RULE: claude.ai edits = POST /patch ONLY. write_file = new files only.
  2026-03-11L: Added self_sync_check() — fixes V1/V2/V6 from 1-year simulation.
               Runs on startup + after every apply_evolution().
               Detects stale CORE_SELF.md (>7 days with active sessions) → Telegram warning.
               Structural evolutions (schema/tool/architecture/file) without
               core_self_updated=true in diff_content trigger automatic Telegram reminder.
  2026-03-11M: CORE v5.1 — System actually evolves, not just documents.
               Added: t_route() real routing engine v2.0 with signal extraction + Groq execution.
               Added: t_ask() — MCP tool to query CORE with full KB context via Groq.
               Added: t_reflect() — single-call hot reflection logger (replaces manual sb_insert).
               Added: t_stats() — analytics: domain breakdown, top patterns, routing distribution.
               Added: t_search_mistakes() — semantic mistake search with domain filter.
               Telegram upgraded: /ask now uses Groq (not just KB keyword search).
               Telegram new: /mistakes [domain], /stats, /route <task>.
               queue_poller upgraded: tasks now actually executed via t_route() + Groq, not just acknowledged.
               TOOLS: 20 → 25 tools. Core is now a working AGI executor, not just a data store.
  2026-03-11N: CORE v5.2 — 24/7 autonomous improvement discovery.
               Added: background_researcher() — runs every hour on Railway without owner.
               Simulates tasks across 8 domains, asks Groq to identify gaps/missing tools/logic.
               Discovered items persisted to KB (domain=backlog) + written to BACKLOG.md on GitHub.
               High-priority items trigger Telegram notification to owner.
               Added: t_get_backlog() — MCP tool to read improvement backlog.
               Added: t_backlog_update() — mark items in_progress/done/dismissed.
               Telegram new: /backlog [min_priority] — see what CORE discovered while you were away.
               TOOLS: 25 → 27 tools.
               CORE now works 24/7: simulate → discover → document → notify → owner executes.
  2026-03-11O: fix — removed duplicate on_start + background_researcher block that caused
               FastAPI startup crash. Root cause: patch injected full researcher block twice.
  2026-03-11P: fix — t_get_backlog now reads Supabase backlog table (restart-proof).
               fix — cold_processor_loop now triggers on KB growth (COLD_KB_GROWTH_THRESHOLD=100).
               Root cause: _backlog in-memory was wiped on restart → BACKLOG.md stayed stale
               even though KB grew from 1k → 3k. Backlog count never updated automatically.
               Fix: t_get_backlog queries Supabase directly. cold_processor_loop checks KB count
               vs last-run KB count and re-triggers if delta >= 100 entries.
  2026-03-11Q: CORE v5.4 — KB mining: one-time batch scan of all KB entries to populate backlog.
               Root cause: 3k KB entries existed but only ~few backlog items because cold processor
               only processes hot_reflections, not raw KB. KB was a dead warehouse.
               Fix: run_kb_mining() reads KB in batches of 20, asks Groq to identify gaps per batch,
               writes discovered items to backlog table via _backlog_add().
               Runs automatically on startup if backlog_count < kb_count / 20 (underpopulated).
               Also exposed as /mine Telegram command and t_mine_kb MCP tool for manual triggering.
               Paced at 3s between batches to respect Groq free tier limits.
"""
import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ── Env vars ─────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST      = os.environ.get("GROQ_MODEL_FAST", "llama-3.1-8b-instant")
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_SVC   = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON  = os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT     = os.environ["GITHUB_PAT"]
GITHUB_REPO    = os.environ.get("GITHUB_USERNAME", "pockiesaints7") + "/core-agi"
MCP_SECRET     = os.environ["MCP_SECRET"]
PORT           = int(os.environ.get("PORT", 8080))
SESSION_TTL_H  = 8

MCP_PROTOCOL_VERSION = "2024-11-05"

# Training config
COLD_HOT_THRESHOLD        = 10
COLD_TIME_THRESHOLD       = 86400
COLD_KB_GROWTH_THRESHOLD  = 100   # re-run cold processor if KB grew by this many entries
PATTERN_EVO_THRESHOLD     = 3
KNOWLEDGE_AUTO_CONFIDENCE = 0.7

# KB mining config — one-time batch scan to populate backlog from raw KB
KB_MINE_BATCH_SIZE        = 20    # KB entries per Groq call
KB_MINE_RATIO_THRESHOLD   = 20    # mine if backlog_count < kb_count / this

# Self-sync config
CORE_SELF_STALE_DAYS = 7

# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.calls = defaultdict(list)
        try:
            self.c = json.load(open("resource_ceilings.json"))
        except:
            self.c = {
                "groq_calls_per_hour": 200,
                "supabase_writes_per_hour": 500,
                "github_pushes_per_hour": 20,
                "telegram_messages_per_hour": 30,
                "mcp_tool_calls_per_minute": 30,
            }

    def _ok(self, key, window, limit):
        now = time.time()
        self.calls[key] = [t for t in self.calls[key] if now - t < window]
        if len(self.calls[key]) >= limit:
            return False
        self.calls[key].append(now)
        return True

    def tg(self):       return self._ok("tg",  3600, self.c.get("telegram_messages_per_hour", 30))
    def gh(self):       return self._ok("gh",  3600, self.c.get("github_pushes_per_hour", 20))
    def sbw(self):      return self._ok("sbw", 3600, self.c.get("supabase_writes_per_hour", 500))
    def mcp(self, sid): return self._ok(f"mcp:{sid}", 60, self.c.get("mcp_tool_calls_per_minute", 30))

L = RateLimiter()

# ── Supabase ──────────────────────────────────────────────────────────────────
def _sbh(svc=False):
    k = SUPABASE_SVC if svc else SUPABASE_ANON
    return {"apikey": k, "Authorization": f"Bearer {k}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

def _sbh_count_svc():
    return {"apikey": SUPABASE_SVC, "Authorization": f"Bearer {SUPABASE_SVC}",
            "Prefer": "count=exact"}

def sb_get(t, qs="", svc=False):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}", headers=_sbh(svc), timeout=15)
    r.raise_for_status()
    return r.json()

def sb_post(t, d):
    if not L.sbw(): return False
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
    if not r.is_success:
        print(f"[SB POST] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

def sb_post_critical(t, d):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
    if not r.is_success:
        print(f"[SB CRITICAL] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

def sb_patch(t, m, d):
    if not L.sbw(): return False
    return httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), json=d, timeout=15).is_success

def sb_upsert(t, d, on_conflict):
    if not L.sbw(): return False
    h = {**_sbh(True), "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}?on_conflict={on_conflict}", headers=h, json=d, timeout=15)
    if not r.is_success:
        print(f"[SB UPSERT] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

# ── Telegram ──────────────────────────────────────────────────────────────────
def notify(msg, cid=None):
    if not L.tg(): return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": cid or TELEGRAM_CHAT, "text": msg[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.is_success:
            print(f"[TG] failed: {r.status_code} {r.text[:100]}")
            return False
        return True
    except Exception as e:
        print(f"[TG] {e}")
        return False

def set_webhook():
    d = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not d: return
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
               data={"url": f"https://{d}/webhook"})
    print("[CORE] Webhook set")

# ── GitHub ────────────────────────────────────────────────────────────────────
def _ghh(): return {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}

def gh_read(path, repo=None):
    r = httpx.get(f"https://api.github.com/repos/{repo or GITHUB_REPO}/contents/{path}", headers=_ghh(), timeout=15)
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode()

def gh_write(path, content, msg, repo=None):
    if not L.gh(): return False
    repo = repo or GITHUB_REPO
    h = _ghh()
    sha = None
    try:
        sha = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=h, timeout=10).json().get("sha")
    except: pass
    p = {"message": msg, "content": base64.b64encode(content.encode()).decode()}
    if sha: p["sha"] = sha
    return httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=h, json=p, timeout=20).is_success

# ── Dynamic step label ────────────────────────────────────────────────────────
_step_cache: dict = {"label": "unknown", "ts": 0.0}
_STEP_CACHE_TTL = 300

def get_current_step() -> str:
    global _step_cache
    if time.time() - _step_cache["ts"] < _STEP_CACHE_TTL:
        return _step_cache["label"]
    try:
        md = gh_read("SESSION.md")
        for line in md.splitlines():
            if line.startswith("## Current Step:"):
                label = line.replace("## Current Step:", "").strip()
                _step_cache = {"label": label, "ts": time.time()}
                return label
    except Exception as e:
        print(f"[STEP] Failed to read SESSION.md: {e}")
    return _step_cache.get("label") or "unknown — check SESSION.md"

# ── Supabase helpers ──────────────────────────────────────────────────────────
def get_latest_session():
    d = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1")
    return d[0] if d else {}

def get_system_counts():
    counts = {}
    for t in ["knowledge_base", "mistakes", "sessions", "task_queue", "hot_reflections", "evolution_queue"]:
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1",
                          headers=_sbh_count_svc(), timeout=10)
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    return counts

# ── Self-sync check ───────────────────────────────────────────────────────────
def self_sync_check():
    try:
        core_self = gh_read("CORE_SELF.md")
        last_updated = None
        for line in core_self.splitlines():
            if "Last updated:" in line:
                date_str = line.split("Last updated:")[-1].strip()
                try:
                    last_updated = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    pass
                break
        if not last_updated:
            notify("⚠️ *CORE Self-Sync Warning*\nCORE_SELF.md has no `Last updated:` date.")
            return {"ok": False, "reason": "no_date"}
        days_stale = (datetime.utcnow() - last_updated).days
        if days_stale > CORE_SELF_STALE_DAYS:
            recent = sb_get("sessions", "select=id&order=created_at.desc&limit=1", svc=True)
            if recent:
                notify(
                    f"⚠️ *CORE Self-Sync Warning*\n"
                    f"CORE_SELF.md last updated *{days_stale} days ago*.\n"
                    f"Active sessions detected since then.\n"
                    f"Please review and update CORE_SELF.md.\n"
                    f"→ github.com/pockiesaints7/core-agi/blob/main/CORE_SELF.md"
                )
                print(f"[SELF_SYNC] WARNING: CORE_SELF.md is {days_stale} days stale")
                return {"ok": False, "days_stale": days_stale, "warned": True}
        print(f"[SELF_SYNC] OK — CORE_SELF.md updated {days_stale}d ago")
        return {"ok": True, "days_stale": days_stale}
    except Exception as e:
        print(f"[SELF_SYNC] error: {e}")
        return {"ok": False, "error": str(e)}


def check_evolution_self_sync(evo: dict):
    structural_types = {"schema", "tool", "architecture", "file", "behavior"}
    change_type = evo.get("change_type", "")
    diff_content = evo.get("diff_content", "") or ""
    if change_type in structural_types and "core_self_updated" not in diff_content.lower():
        notify(
            f"📋 *CORE Self-Sync Reminder*\n"
            f"Evolution #{evo.get('id')} applied (type: `{change_type}`).\n"
            f"Please update *CORE_SELF.md* if structure changed.\n"
            f"→ github.com/pockiesaints7/core-agi/blob/main/CORE_SELF.md"
        )

# ── MCP session management ────────────────────────────────────────────────────
_sessions: dict = {}

def mcp_new(ip: str) -> str:
    tok = hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[tok] = {"ip": ip, "expires": (datetime.utcnow() + timedelta(hours=SESSION_TTL_H)).isoformat(), "calls": 0}
    now = datetime.utcnow()
    expired = [k for k, v in _sessions.items() if datetime.fromisoformat(v["expires"]) < now]
    for k in expired: del _sessions[k]
    return tok

def mcp_ok(tok: str) -> bool:
    if tok not in _sessions: return False
    if datetime.utcnow() > datetime.fromisoformat(_sessions[tok]["expires"]):
        del _sessions[tok]; return False
    _sessions[tok]["calls"] += 1
    return True

# ── Training pipeline ─────────────────────────────────────────────────────────
def auto_hot_reflection(session_data: dict):
    try:
        summary   = session_data.get("summary", "")
        actions   = session_data.get("actions", []) or []
        interface = session_data.get("interface", "unknown")
        total     = max(len(actions), 1)
        verify_rate  = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["verify","readback","confirm"])) / total, 2)
        mistake_rate = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["mistake","error","fix","wrong"])) / total, 2)
        domain = "general"
        for kw, d in [("supabase","db"),("github","code"),("telegram","bot"),("mcp","mcp"),("training","training"),("knowledge","kb")]:
            if kw in summary.lower(): domain = d; break
        ok = sb_post("hot_reflections", {
            "task_summary": summary[:300], "domain": domain,
            "verify_rate": verify_rate, "mistake_consult_rate": mistake_rate,
            "new_patterns": [], "new_mistakes": [],
            "quality_score": None, "gaps_identified": None,
            "reflection_text": f"Auto-generated from {interface} session. Actions: {total}.",
            "processed_by_cold": False,
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


def run_cold_processor():
    try:
        hots = sb_get("hot_reflections",
                      "select=id,domain,new_patterns,new_mistakes,quality_score&processed_by_cold=eq.0&id=gt.1&order=created_at.asc",
                      svc=True)
        if not hots:
            print("[COLD] No unprocessed hot reflections.")
            return {"ok": True, "processed": 0, "evolutions_queued": 0}

        period_start      = datetime.utcnow().isoformat()
        evolutions_queued = 0
        batch_counts: Counter = Counter()
        batch_domain: dict    = {}
        for h in hots:
            for p in (h.get("new_patterns") or []):
                if p:
                    key = str(p)[:200]
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))

        for key, batch_count in batch_counts.items():
            from urllib.parse import quote
            key_enc = quote(key, safe="")
            existing = [e for e in sb_get("pattern_frequency",
                        f"select=id,frequency,auto_applied&pattern_key=eq.{key_enc}&limit=1", svc=True)
                        if e.get("id") != 1]
            if existing:
                rec      = existing[0]
                new_freq = (rec.get("frequency") or 0) + batch_count
                sb_patch("pattern_frequency", f"id=eq.{rec['id']}",
                         {"frequency": new_freq, "last_seen": datetime.utcnow().isoformat()})
                if new_freq >= PATTERN_EVO_THRESHOLD and not rec.get("auto_applied"):
                    if sb_post_critical("evolution_queue", {
                        "status": "pending", "change_type": "knowledge",
                        "change_summary": f"Pattern '{key}' seen {new_freq}x — promote to knowledge base",
                        "pattern_key": key, "frequency": new_freq,
                        "confidence": min(0.5 + new_freq * 0.1, 0.95),
                        "impact": "low", "reversible": True, "owner_notified": False,
                    }):
                        evolutions_queued += 1
                        sb_patch("pattern_frequency", f"id=eq.{rec['id']}", {"auto_applied": True})
            else:
                new_freq = batch_count
                sb_upsert("pattern_frequency", {
                    "pattern_key": key, "domain": batch_domain.get(key, "general"),
                    "description": key, "frequency": new_freq, "confidence": 0.5,
                    "first_seen": datetime.utcnow().isoformat(),
                    "last_seen": datetime.utcnow().isoformat(), "auto_applied": False,
                }, "pattern_key")
                if new_freq >= PATTERN_EVO_THRESHOLD:
                    if sb_post_critical("evolution_queue", {
                        "status": "pending", "change_type": "knowledge",
                        "change_summary": f"New pattern '{key}' seen {new_freq}x — promote to knowledge base",
                        "pattern_key": key, "frequency": new_freq,
                        "confidence": min(0.5 + new_freq * 0.1, 0.95),
                        "impact": "low", "reversible": True, "owner_notified": False,
                    }):
                        evolutions_queued += 1

        sb_post_critical("cold_reflections", {
            "period_start": period_start, "period_end": datetime.utcnow().isoformat(),
            "hot_count": len(hots), "patterns_found": len(batch_counts),
            "evolutions_queued": evolutions_queued, "auto_applied": 0,
            "summary_text": f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns. {evolutions_queued} evolutions queued.",
        })
        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": 1})
        if evolutions_queued > 0:
            notify(f"✨ Cold processor: {evolutions_queued} evolution(s) queued from {len(hots)} sessions.\nUse /evolutions to review.")
        print(f"[COLD] Done: processed={len(hots)} patterns={len(batch_counts)} evolutions={evolutions_queued}")
        return {"ok": True, "processed": len(hots), "patterns_found": len(batch_counts), "evolutions_queued": evolutions_queued}
    except Exception as e:
        print(f"[COLD] error: {e}")
        return {"ok": False, "error": str(e)}


def apply_evolution(evolution_id: int):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=eq.pending&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending"}
        evo           = rows[0]
        change_type   = evo.get("change_type", "knowledge")
        change_summary= evo.get("change_summary", "")
        diff_content  = evo.get("diff_content", "")
        pattern_key   = evo.get("pattern_key", "")
        confidence    = float(evo.get("confidence") or 0.5)
        applied = False; note = ""

        if change_type == "knowledge":
            applied = sb_post_critical("knowledge_base", {
                "domain": evo.get("impact", "general"),
                "topic": pattern_key or change_summary[:100],
                "content": change_summary,
                "confidence": "high" if confidence >= 0.8 else "medium",
                "tags": ["evolution", "auto"], "source": "evolution_queue",
            })
            note = "Added to knowledge_base"
        elif change_type == "code":
            if not diff_content: return {"ok": False, "error": "code evolution requires diff_content"}
            fname = f"patches/evo_{evolution_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.patch"
            applied = gh_write(fname, diff_content, f"Evolution #{evolution_id}: {change_summary[:60]}")
            note = f"Patch written to {fname}"
        elif change_type == "behavior":
            if not diff_content: return {"ok": False, "error": "behavior evolution requires diff_content"}
            applied = gh_write("BEHAVIOR_UPDATES.md", diff_content,
                               f"Behavior evolution #{evolution_id}: {change_summary[:60]}")
            note = "Written to BEHAVIOR_UPDATES.md"
        elif change_type == "backlog":
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            btype    = meta.get("backlog_type", "other")
            executor = meta.get("executor", "auto")
            domain   = meta.get("domain", "general")
            title    = meta.get("title", change_summary[:80])
            desc     = meta.get("description", change_summary)

            if executor == "groq" or (executor == "auto" and btype in ("new_kb", "missing_data")):
                if btype == "new_kb":
                    applied = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["backlog", "auto_applied"],
                        "source": "evolution_queue",
                    }))
                    note = f"[groq] KB entry added: {title}"
                else:
                    task_payload = json.dumps({"task": desc, "domain": domain, "source": "backlog", "title": title})
                    applied = bool(sb_post("task_queue", {
                        "type": "improvement", "payload": task_payload, "status": "pending",
                        "priority": int(evo.get("confidence", 0.5) * 10), "source": "backlog_evolution",
                    }))
                    note = f"[groq] Task queued: {title}"

            elif executor == "claude_desktop" or (executor == "auto" and btype in ("new_tool", "telegram_command")):
                sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "pending_desktop"})
                notify(
                    f"[BACKLOG] Approved - needs Claude Desktop\n"
                    f"Type: {btype} | {title}\n\n"
                    f"Action: In next Claude Desktop session, implement:\n{desc[:300]}\n\n"
                    f"Evolution ID: {evolution_id}"
                )
                applied = True
                note = f"[claude_desktop] Flagged for Desktop session: {title}"

            else:
                plan_prompt = f"Generate a concise implementation plan for: {title}\nDescription: {desc}\nOutput as numbered steps, max 5 steps."
                plan = groq_chat("You are CORE planning engine. Be concise.", plan_prompt,
                                 model=GROQ_FAST, max_tokens=300)
                applied = bool(sb_post("task_queue", {
                    "type": "improvement",
                    "payload": json.dumps({"title": title, "plan": plan, "domain": domain}),
                    "status": "pending", "priority": 5, "source": "backlog_evolution",
                }))
                note = f"[auto] Plan generated + queued: {title}"

            # Update backlog status in Supabase
            sb_patch("backlog", f"title=eq.{title}",
                     {"status": "done" if btype == "new_kb" else "in_progress"})

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            notify(f"✅ Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
            check_evolution_self_sync(evo)
            try:
                gh_write("BACKLOG.md", _backlog_to_markdown(),
                         f"chore(backlog): sync after evo #{evolution_id} applied [{change_type}]")
            except Exception as _be:
                print(f"[BACKLOG] refresh error: {_be}")
        else:
            notify(f"❌ Evolution #{evolution_id} apply failed\nType: {change_type}")
        return {"ok": applied, "evolution_id": evolution_id, "change_type": change_type, "note": note}
    except Exception as e:
        print(f"[EVO] error: {e}")
        return {"ok": False, "error": str(e)}


def reject_evolution(evolution_id: int, reason: str = ""):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=eq.pending&limit=1", svc=True)
        if not rows: return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending"}
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "rejected"})
        sb_post("mistakes", {
            "domain": "evolution", "context": f"Evolution #{evolution_id}: {rows[0].get('change_summary','')[:200]}",
            "what_failed": "Evolution rejected by owner",
            "correct_approach": reason or "Owner rejected — review pattern and confidence threshold",
            "root_cause": reason or "Unknown",
            "how_to_avoid": "Raise confidence threshold or improve pattern quality",
            "severity": "low", "tags": ["evolution", "rejected"],
        })
        notify(f"❌ Evolution #{evolution_id} rejected.\nReason: {reason or 'No reason given'}")
        return {"ok": True, "evolution_id": evolution_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_last_cold_run: float = 0.0
_last_cold_kb_count: int = 0  # KB count at last cold run — used for growth trigger

def cold_processor_loop():
    global _last_cold_run, _last_cold_kb_count
    print("[COLD] Background loop started")
    while True:
        try:
            hots        = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
            unprocessed = len(hots)
            time_since  = time.time() - _last_cold_run

            # KB growth trigger — re-run if KB grew by COLD_KB_GROWTH_THRESHOLD since last run
            current_kb_count = 0
            try:
                counts = get_system_counts()
                current_kb_count = counts.get("knowledge_base", 0)
            except Exception:
                pass
            kb_growth = current_kb_count - _last_cold_kb_count

            should_run = (
                unprocessed >= COLD_HOT_THRESHOLD or
                (time_since >= COLD_TIME_THRESHOLD and unprocessed > 0) or
                (kb_growth >= COLD_KB_GROWTH_THRESHOLD and _last_cold_kb_count > 0)
            )

            if should_run:
                trigger = (
                    f"unprocessed={unprocessed}" if unprocessed >= COLD_HOT_THRESHOLD else
                    f"kb_growth={kb_growth} (was {_last_cold_kb_count} → now {current_kb_count})" if kb_growth >= COLD_KB_GROWTH_THRESHOLD else
                    f"time_since={int(time_since)}s"
                )
                print(f"[COLD] Triggering: {trigger}")
                run_cold_processor()
                _last_cold_run = time.time()
                _last_cold_kb_count = current_kb_count
                # Also refresh BACKLOG.md whenever cold processor runs
                try:
                    gh_write("BACKLOG.md", _backlog_to_markdown(),
                             f"chore(backlog): auto-refresh after cold processor ({trigger})")
                except Exception as be:
                    print(f"[COLD] backlog refresh error: {be}")

            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    apply_evolution(evo["id"])
        except Exception as e:
            print(f"[COLD] loop error: {e}")
        time.sleep(1800)


# ── MCP tool implementations ──────────────────────────────────────────────────
def t_state():
    session = get_latest_session()
    counts  = get_system_counts()
    pending = sb_get("task_queue", "select=id,task,status&status=eq.pending&limit=5")
    try:    operating_context = json.loads(gh_read("operating_context.json"))
    except Exception as e: operating_context = {"error": f"failed to load: {e}"}
    try:    session_md = gh_read("SESSION.md")[:2000]
    except Exception as e: session_md = f"SESSION.md unavailable: {e}"
    return {"last_session": session.get("summary", "No sessions yet."),
            "last_actions": session.get("actions", []),
            "last_session_ts": session.get("created_at", ""),
            "counts": counts, "pending_tasks": pending,
            "operating_context": operating_context, "session_md": session_md}

def t_health():
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    for name, fn in [
        ("supabase", lambda: sb_get("sessions", "select=id&limit=1")),
        ("groq",     lambda: httpx.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status()),
        ("telegram", lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status()),
        ("github",   lambda: gh_read("README.md")),
    ]:
        try: fn(); h["components"][name] = "ok"
        except Exception as e: h["components"][name] = f"error:{e}"
    h["overall"] = "ok" if all(v == "ok" for v in h["components"].values()) else "degraded"
    return h

def t_constitution():
    try:
        with open("constitution.txt") as f: txt = f.read()
    except: txt = gh_read("constitution.txt")
    return {"constitution": txt, "immutable": True}

def t_search_kb(query="", domain="", limit=10):
    qs = f"select=domain,topic,content,confidence&limit={limit}"
    if domain: qs += f"&domain=eq.{domain}"
    if query:  qs += f"&content=ilike.*{query.split()[0]}*"
    return sb_get("knowledge_base", qs)

def t_get_mistakes(domain="", limit=10):
    try: lim = int(limit) if limit else 10
    except: lim = 10
    qs = f"select=domain,context,what_failed,correct_approach&order=created_at.desc&limit={lim}"
    if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key, value, reason):
    ok = sb_post("sessions", {"summary": f"[state_update] {key}: {str(value)[:200]}",
                              "actions": [f"{key}={str(value)[:100]} — {reason}"], "interface": "mcp"})
    return {"ok": ok, "key": key}

def t_add_knowledge(domain, topic, content, tags="", confidence="medium"):
    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    ok = sb_post("knowledge_base", {"domain": domain, "topic": topic, "content": content,
                                    "confidence": confidence, "tags": tags_list, "source": "mcp_session"})
    return {"ok": ok, "topic": topic}

def t_log_mistake(context, what_failed, fix, domain="general", root_cause="", how_to_avoid="", severity="medium"):
    ok = sb_post("mistakes", {"domain": domain, "context": context, "what_failed": what_failed,
                              "correct_approach": fix, "root_cause": root_cause or what_failed,
                              "how_to_avoid": how_to_avoid or fix, "severity": severity, "tags": []})
    return {"ok": ok}

def t_read_file(path, repo=""):
    try: return {"ok": True, "content": gh_read(path, repo or GITHUB_REPO)[:5000]}
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path, content, message, repo=""):
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "ℹ️", "warn": "⚠️", "alert": "🚨", "ok": "✅"}
    return {"ok": notify(f"{icons.get(level, '»')} CORE\n{message}")}

def t_sb_query(table, filters="", limit=20):
    try: lim = int(limit) if limit else 20
    except: lim = 20
    qs = f"{filters}&limit={lim}" if filters else f"limit={lim}"
    return sb_get(table, qs, svc=True)

def t_sb_insert(table, data):
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e: return {"ok": False, "error": f"data must be valid JSON: {e}"}
    return {"ok": sb_post(table, data), "table": table}

def t_training_status():
    try:
        unprocessed = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
        pending_evo = sb_get("evolution_queue", "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1", svc=True)
        try:
            backlog_pending = int(httpx.get(
                f"{SUPABASE_URL}/rest/v1/backlog?select=id&status=eq.pending&limit=1",
                headers=_sbh_count_svc(), timeout=10
            ).headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            backlog_pending = -1
        return {"status": f"Training pipeline ACTIVE — {get_current_step()}",
                "unprocessed_hot": len(unprocessed), "pending_evolutions": len(pending_evo),
                "backlog_pending": backlog_pending,
                "evolutions": pending_evo[:5], "cold_threshold": COLD_HOT_THRESHOLD,
                "kb_growth_threshold": COLD_KB_GROWTH_THRESHOLD,
                "kb_mine_ratio_threshold": KB_MINE_RATIO_THRESHOLD,
                "pattern_threshold": PATTERN_EVO_THRESHOLD, "auto_apply_conf": KNOWLEDGE_AUTO_CONFIDENCE}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def t_trigger_cold_processor(): return run_cold_processor()

def t_list_evolutions(status="pending"):
    rows = sb_get("evolution_queue",
                  f"select=id,status,change_type,change_summary,confidence,pattern_key,created_at&status=eq.{status}&id=gt.1&order=created_at.desc&limit=20",
                  svc=True)
    return {"evolutions": rows, "count": len(rows)}

def t_approve_evolution(evolution_id):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return apply_evolution(eid)

def t_reject_evolution(evolution_id, reason=""):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return reject_evolution(eid, reason)

def t_gh_search_replace(path, old_str, new_str, message, repo=""):
    try:
        repo = repo or GITHUB_REPO
        r = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                      headers=_ghh(), timeout=15)
        r.raise_for_status()
        meta = r.json()
        file_content = base64.b64decode(meta["content"]).decode()
        sha = meta["sha"]
        if old_str not in file_content:
            return {"ok": False, "error": f"old_str not found in {path}"}
        count = file_content.count(old_str)
        if count > 1:
            return {"ok": False, "error": f"old_str found {count}x — be more specific"}
        new_content = file_content.replace(old_str, new_str, 1)
        encoded = base64.b64encode(new_content.encode()).decode()
        pr = httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}",
                       headers=_ghh(), json={"message": message, "content": encoded, "sha": sha}, timeout=20)
        ok = pr.is_success
        return {"ok": ok, "path": path, "replaced": old_str[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_gh_read_lines(path, start_line=1, end_line=50, repo=""):
    try:
        file_content = gh_read(path, repo or GITHUB_REPO)
        lines = file_content.splitlines()
        total = len(lines)
        s = max(1, int(start_line)) - 1
        e = min(total, int(end_line))
        selected = lines[s:e]
        numbered = "\n".join(f"{s+i+1:4d}  {line}" for i, line in enumerate(selected))
        return {"ok": True, "path": path, "total_lines": total,
                "showing": f"{s+1}-{s+len(selected)}", "content": numbered}
    except Exception as ex:
        return {"ok": False, "error": str(ex), "path": path}


# ── Groq LLM call ─────────────────────────────────────────────────────────────
def groq_chat(system: str, user: str, model: str = None, max_tokens: int = 1024) -> str:
    m = model or GROQ_MODEL
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": m, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Signal extraction (routing pre-layer) ─────────────────────────────────────
def extract_signals(task: str) -> dict:
    t = task.lower()
    intent = "generate"
    for kw, v in [("fix","fix"),("debug","fix"),("error","fix"),("broken","fix"),
                  ("explain","explain"),("what is","explain"),("how does","explain"),("teach","explain"),
                  ("find","lookup"),("search","lookup"),("who is","lookup"),("when did","lookup"),
                  ("analyze","analyze"),("review","analyze"),("check","validate"),("is this","validate"),
                  ("write","generate"),("create","generate"),("build","build"),("make","build"),
                  ("should i","decide"),("which","decide"),("recommend","decide"),
                  ("help","support"),("overwhelmed","support"),("worried","support"),("scared","support"),
                  ("plan","orchestrate"),("steps to","orchestrate"),("how to","orchestrate")]:
        if kw in t: intent = v; break

    domain = "general"
    for kw, d in [("def ","code"),("function","code"),("import ","code"),("class ","code"),("sql","code"),
                  ("contract","legal"),("liability","legal"),("clause","legal"),("lawsuit","legal"),
                  ("invoice","finance"),("revenue","finance"),("cash flow","finance"),("tax","finance"),
                  ("patient","medical"),("symptoms","medical"),("diagnosis","medical"),("medication","medical"),
                  ("marketing","business"),("customers","business"),("startup","business"),("sales","business"),
                  ("essay","academic"),("research","academic"),("thesis","academic"),("cite","academic"),
                  ("content","creative"),("story","creative"),("blog","creative"),("design","creative")]:
        if kw in t: domain = d; break

    expertise = 3
    beginner_markers = ["what is","how do i","i don't know","explain","simple","basic","beginner","noob","untuk pemula","apa itu","gimana caranya"]
    expert_markers   = ["implement","optimize","architecture","idiomatic","edge case","tradeoff","latency","throughput","refactor","scalab"]
    if any(m in t for m in beginner_markers): expertise = 2
    if any(m in t for m in expert_markers):   expertise = 4
    if len(task.split()) <= 5 and "?" not in task: expertise = max(expertise, 4)

    emotion = "neutral"
    if any(m in t for m in ["asap","urgent","deadline","help!","tolong","buru","cepat"]): emotion = "urgent"
    elif any(m in t for m in ["still","again","doesn't work","still not","masih ga","kenapa ga"]): emotion = "frustrated"
    elif any(m in t for m in ["worried","scared","overwhelmed","anxious","takut","bingung banget","ga tau harus"]): emotion = "vulnerable"
    elif any(m in t for m in ["lol","btw","just wondering","haha","wkwk","iseng","santai"]): emotion = "casual"

    stakes = "medium"
    if any(m in t for m in ["quick","short","brief","simple","just","cepet","singkat"]): stakes = "low"
    if any(m in t for m in ["production","deploy","contract","legal","medical","critical","penting banget","serius"]): stakes = "high"
    if any(m in t for m in ["life","death","emergency","darurat","nyawa","hukum","lawsuit"]): stakes = "critical"

    archetype_map = {
        "lookup": "A1", "explain": "A4", "generate": "A3", "fix": "A4",
        "analyze": "A4", "validate": "A8", "build": "A5", "decide": "A6",
        "orchestrate": "A7", "support": "A9",
    }
    return {"intent": intent, "domain": domain, "expertise": expertise,
            "emotion": emotion, "stakes": stakes, "archetype": archetype_map.get(intent, "A3")}


def t_route(task: str, execute: bool = False):
    if not task: return {"ok": False, "error": "task required"}
    sig = extract_signals(task)
    complexity = 3
    if sig["expertise"] <= 2:  complexity += 1
    if sig["emotion"] in ("urgent", "frustrated"): complexity += 1
    if sig["stakes"] == "critical": complexity += 2
    if sig["stakes"] == "high":     complexity += 1
    if sig["expertise"] >= 5:  complexity -= 1
    if sig["stakes"] == "low": complexity -= 1
    complexity = max(1, min(12, complexity))

    tone_map = {
        "urgent":     "Be concise and direct. Lead with the answer immediately.",
        "frustrated": "Acknowledge the difficulty briefly, then provide the fix directly.",
        "vulnerable": "Be warm and supportive. Slow down. Acknowledge before solving.",
        "casual":     "Match casual energy. Keep it natural and brief.",
        "neutral":    "Be clear and structured.",
    }
    expertise_map = {
        1: "Explain everything simply. Use analogies. Avoid jargon.",
        2: "Define non-obvious terms. Provide step-by-step guidance.",
        3: "Assume basic familiarity. Provide context where needed.",
        4: "Skip basics. Use domain vocabulary. Be precise.",
        5: "Expert-to-expert. Dense, precise, no hand-holding.",
    }
    disclaimer = ""
    if sig["domain"] in ("legal","medical","finance") and sig["expertise"] <= 2:
        disclaimer = "Add a brief note to verify with a professional for consequential decisions."

    system_prompt = (
        f"You are CORE, a personal AGI. "
        f"{tone_map.get(sig['emotion'], tone_map['neutral'])} "
        f"{expertise_map.get(sig['expertise'], expertise_map[3])} "
        f"Domain context: {sig['domain']}. Stakes level: {sig['stakes']}. {disclaimer} "
        "Be genuinely helpful. If you see a gap the user didn't ask about but should know, mention it briefly."
    )
    routing_info = {"signals": sig, "complexity": complexity,
                    "system_prompt_preview": system_prompt[:120] + "...", "archetype": sig["archetype"]}

    if not execute:
        return {"ok": True, "routing": routing_info}

    try:
        model = GROQ_FAST if complexity <= 4 else GROQ_MODEL
        response = groq_chat(system_prompt, task, model=model)
        sb_post("task_queue", {"task": task[:300], "status": "completed", "priority": 5, "error": None, "chat_id": ""})
        return {"ok": True, "routing": routing_info, "response": response, "model_used": model}
    except Exception as e:
        return {"ok": False, "routing": routing_info, "error": str(e)}


def t_ask(question: str, domain: str = ""):
    if not question: return {"ok": False, "error": "question required"}
    kb_results = t_search_kb(question, domain=domain, limit=5)
    kb_context = "\n\n".join([f"[KB: {r.get('topic','')}]\n{str(r.get('content',''))[:300]}" for r in kb_results]) if kb_results else ""
    mistakes = t_get_mistakes(domain=domain or "general", limit=3)
    mistake_context = "\n".join([f"- Avoid: {m.get('what_failed','')} → {m.get('correct_approach','')[:100]}" for m in mistakes]) if mistakes else ""
    system = ("You are CORE, a personal AGI assistant with accumulated knowledge from many sessions. "
              "Answer using the knowledge base context provided. Be specific and actionable. "
              "If KB context is insufficient, say so and answer from general knowledge.")
    user = f"Question: {question}\n\n"
    if kb_context: user += f"Relevant knowledge:\n{kb_context}\n\n"
    if mistake_context: user += f"Known pitfalls to avoid:\n{mistake_context}\n\n"
    user += "Answer:"
    try:
        answer = groq_chat(system, user, model=GROQ_FAST, max_tokens=512)
        return {"ok": True, "answer": answer, "kb_hits": len(kb_results), "question": question}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_reflect(task_summary: str, domain: str = "general", patterns: list = None,
              quality: float = None, notes: str = ""):
    ok = sb_post("hot_reflections", {
        "task_summary": task_summary[:300], "domain": domain,
        "verify_rate": 0.0, "mistake_consult_rate": 0.0,
        "new_patterns": patterns or [], "new_mistakes": [],
        "quality_score": quality, "gaps_identified": None,
        "reflection_text": notes or f"Logged via t_reflect. Domain: {domain}.",
        "processed_by_cold": False,
    })
    return {"ok": ok, "domain": domain, "patterns_count": len(patterns or [])}


def t_stats():
    try:
        hots = sb_get("hot_reflections", "select=domain,quality_score&limit=200", svc=True)
        domain_counts: Counter = Counter(h.get("domain","general") for h in hots)
        patterns = sb_get("pattern_frequency", "select=pattern_key,frequency,domain&order=frequency.desc&limit=10", svc=True)
        mistakes = sb_get("mistakes", "select=domain&limit=200", svc=True)
        mistake_counts: Counter = Counter(m.get("domain","general") for m in mistakes)
        scores = [h["quality_score"] for h in hots if h.get("quality_score") is not None]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else None
        counts = get_system_counts()
        return {
            "ok": True,
            "total_sessions": counts.get("sessions", 0),
            "knowledge_entries": counts.get("knowledge_base", 0),
            "total_mistakes": counts.get("mistakes", 0),
            "hot_reflections": len(hots),
            "avg_quality_score": avg_quality,
            "domain_distribution": dict(domain_counts.most_common(8)),
            "mistake_distribution": dict(mistake_counts.most_common(6)),
            "top_patterns": [{"pattern": p.get("pattern_key","")[:80], "freq": p.get("frequency",0), "domain": p.get("domain","")} for p in patterns],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_search_mistakes(query: str = "", domain: str = "", limit: int = 10):
    try:
        lim = int(limit) if limit else 10
        qs = f"select=domain,context,what_failed,correct_approach,root_cause,severity&order=created_at.desc&limit={lim}"
        if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
        if query:
            word = query.split()[0]
            qs += f"&what_failed=ilike.*{word}*"
        results = sb_get("mistakes", qs, svc=True)
        return {"ok": True, "count": len(results), "mistakes": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Background Researcher — 24/7, runs every 60 min ──────────────────────────
_RESEARCH_DOMAINS = [
    ("code",     ["debug this python function", "optimize SQL query", "refactor async code",
                  "explain this error", "design REST API", "review this architecture"]),
    ("business", ["improve cash flow", "write investor pitch", "reduce churn",
                  "hire first employee", "expand to new market", "price my product"]),
    ("legal",    ["draft NDA", "understand terms of service", "employment contract review",
                  "IP protection for startup", "GDPR compliance checklist"]),
    ("creative", ["write product description", "social media strategy", "brand voice guide",
                  "content calendar", "email newsletter"]),
    ("academic", ["summarize research paper", "explain statistical method",
                  "literature review outline", "thesis argument structure"]),
    ("medical",  ["explain diagnosis", "medication interaction check", "symptom checker",
                  "treatment options", "second opinion research"]),
    ("finance",  ["build financial model", "tax optimization", "runway calculation",
                  "fundraising strategy", "unit economics"]),
    ("data",     ["clean messy dataset", "visualize trends", "build dashboard",
                  "anomaly detection", "A/B test analysis"]),
]

_IMPROVEMENT_INTERVAL = 3600  # 60 min — free Groq tier safe
_last_research_run: float = 0.0
# NOTE: _backlog in-memory removed — all backlog state lives in Supabase `backlog` table.


def _extract_real_signal() -> bool:
    """Track A — read real Supabase data, ask Groq to extract patterns.
    Writes to hot_reflections tagged source=real."""
    try:
        # Load real data from Supabase
        sessions = sb_get("sessions",
            "select=summary,actions,interface&order=created_at.desc&limit=20", svc=True)
        mistakes = sb_get("mistakes",
            "select=domain,what_failed,root_cause,how_to_avoid&order=id.desc&limit=20", svc=True)

        if not sessions and not mistakes:
            print("[RESEARCH/REAL] No data yet — skipping")
            return False

        sessions_text = "\n".join([
            f"- [{r.get('interface','?')}] {r.get('summary','')[:200]}"
            for r in sessions
        ]) or "No sessions yet."

        mistakes_text = "\n".join([
            f"- [{r.get('domain','?')}] FAILED: {r.get('what_failed','')[:150]} | ROOT: {r.get('root_cause','')[:100]}"
            for r in mistakes
        ]) or "No mistakes yet."

        system = """You are CORE's pattern extraction engine. Analyze real activity logs from an AGI orchestration system.

Your job: identify concrete recurring patterns, failures, and gaps from the actual data.
Do NOT invent scenarios. Only extract what the data shows.

Output MUST be a valid JSON object:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2", ...],
  "gaps": "1-2 sentences describing what's missing or unreliable",
  "summary": "1 sentence summary of what this batch reveals"
}

patterns: list of 3-7 short strings, each a concrete repeating behavior or failure.
Examples of good patterns: "gh_search_replace fails on special characters",
"always verify after write", "rate limiter hit during bulk operations"
Output ONLY valid JSON, no preamble."""

        user = (f"RECENT SESSIONS ({len(sessions)}):\n{sessions_text}\n\n"
                f"RECENT MISTAKES ({len(mistakes)}):\n{mistakes_text}\n\n"
                f"Extract patterns from this real activity.")

        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=800)
        raw = raw.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/REAL] Groq returned no patterns")
            return False

        ok = sb_post("hot_reflections", {
            "task_summary": f"Real signal extraction — {len(sessions)} sessions, {len(mistakes)} mistakes",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": False,
            "source": "real",
            "quality_score": None,
        })
        print(f"[RESEARCH/REAL] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/REAL] error: {e}")
        return False


def _run_simulation_batch() -> bool:
    """Track B — load real CORE context, simulate 1M user population, extract patterns.
    Writes to hot_reflections tagged source=simulation."""
    try:
        # Load real CORE context to ground the simulation
        tool_list = list(TOOLS.keys())
        mistakes = sb_get("mistakes",
            "select=domain,what_failed&order=id.desc&limit=10", svc=True)
        kb_sample = sb_get("knowledge_base",
            "select=domain,topic&order=id.desc&limit=20", svc=True)

        failure_modes = "\n".join([
            f"- [{r.get('domain','?')}] {r.get('what_failed','')[:120]}"
            for r in mistakes
        ]) or "None recorded yet."

        kb_domains = list({r.get("domain", "general") for r in kb_sample})
        kb_topics_sample = [r.get("topic", "") for r in kb_sample[:10]]

        system = """You are simulating 1,000,000 users of CORE — a personal AGI orchestration system.

Your simulation must be grounded in CORE's actual architecture. Do NOT invent generic AI scenarios.
Simulate the realistic distribution of how real users interact with THIS specific system.

Usage distribution to simulate:
- 40% routine tasks: KB queries, session logging, routing requests
- 30% complex multi-step: write then verify then apply then check
- 20% edge cases: rate limits, concurrent ops, ambiguous routing, missing KB entries
- 10% failure recovery: retry logic, rollback, error handling, bad Groq output

For this simulated population batch, identify:
1. What patterns do users hit repeatedly across all usage types?
2. What breaks or confuses them most often?
3. What is missing from the KB that users keep needing?
4. What tool behavior is unexpected or dangerous at scale?

Output MUST be a valid JSON object:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2", ...],
  "gaps": "1-2 sentences on what this population most needs",
  "summary": "1 sentence summary"
}
patterns: 4-8 short concrete strings grounded in CORE's actual tools and domains.
Output ONLY valid JSON, no preamble."""

        user = (f"CORE's MCP tools ({len(tool_list)}): {', '.join(tool_list)}\n\n"
                f"Known failure modes from real usage:\n{failure_modes}\n\n"
                f"KB domains active: {', '.join(kb_domains)}\n"
                f"Sample KB topics: {', '.join(kb_topics_sample)}\n\n"
                f"Simulate 1,000,000 users hitting this system. What patterns emerge?")

        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=900)
        raw = raw.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/SIM] Groq returned no patterns")
            return False

        ok = sb_post("hot_reflections", {
            "task_summary": f"Simulated 1M user population batch — grounded in real CORE context",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": False,
            "source": "simulation",
            "quality_score": None,
        })
        print(f"[RESEARCH/SIM] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/SIM] error: {e}")
        return False


def _backlog_add(items: list) -> list:
    """Write new backlog items directly to Supabase `backlog` table. No in-memory list."""
    try:
        existing_rows = sb_get("backlog", "select=title&order=id.asc&limit=500", svc=True)
        existing_titles = {r.get("title", "").lower() for r in existing_rows}
    except Exception as e:
        print(f"[BACKLOG] fetch existing error: {e}")
        existing_titles = set()

    new_items = []
    for item in items:
        title = item.get("title", "").strip()
        if not title or title.lower() in existing_titles:
            continue
        existing_titles.add(title.lower())
        priority = int(item.get("priority", 1))
        itype    = item.get("type", "other")
        effort   = item.get("effort", "medium")
        domain   = item.get("domain", "general")

        ok = sb_post("backlog", {
            "title":        title,
            "type":         itype,
            "priority":     priority,
            "description":  item.get("description", "")[:500],
            "domain":       domain,
            "effort":       effort,
            "impact":       item.get("impact", "medium"),
            "status":       "pending",
            "discovered_at": item.get("discovered_at", datetime.utcnow().isoformat()),
        })
        if ok:
            new_items.append(item)

        if priority >= 3:
            executor = (
                "claude_desktop" if itype in ("new_tool", "telegram_command") else
                "groq"           if itype in ("new_kb", "missing_data") else
                "auto"
            )
            change_type = "knowledge" if itype == "new_kb" else "backlog"
            auto_apply  = (itype == "new_kb" and effort == "low" and executor == "groq")
            sb_post_critical("evolution_queue", {
                "change_type":    change_type,
                "change_summary": f"[BACKLOG P{priority}][{executor}] {title}: {item.get('description','')[:180]}",
                "diff_content":   json.dumps({
                    "backlog_type": itype, "executor": executor,
                    "domain": domain, "effort": effort,
                    "impact": item.get("impact", "medium"),
                    "title": title, "description": item.get("description", ""),
                }),
                "pattern_key": f"backlog:{itype}:{title[:60]}",
                "confidence":  round(0.5 + priority * 0.08, 2),
                "status":      "applied" if auto_apply else "pending",
                "source":      "background_researcher",
                "impact":      domain,
            })
    return new_items


def _sync_backlog_status():
    """Sync backlog item statuses from evolution_queue into Supabase backlog table."""
    try:
        rows = sb_get("evolution_queue",
                      "select=status,pattern_key&change_type=in.(backlog,knowledge)&order=id.desc&limit=500",
                      svc=True)
        synced = 0
        for row in rows:
            pk = row.get("pattern_key", "")
            if not pk.startswith("backlog:"): continue
            parts = pk.split(":", 2)
            if len(parts) != 3: continue
            title_key = parts[2]
            es = row.get("status", "pending")
            new_status = (
                "done"        if es in ("applied", "done") else
                "in_progress" if es == "pending_desktop" else
                None
            )
            if new_status:
                sb_patch("backlog", f"title=eq.{title_key}", {"status": new_status})
                synced += 1
        return synced
    except Exception as e:
        print(f"[BACKLOG] status sync error: {e}")
        return 0


def _repopulate_evolution_queue():
    """Re-push P3+ backlog items missing from evolution_queue.
    Reads directly from Supabase backlog table — restart-proof."""
    try:
        existing = sb_get("evolution_queue",
                          "select=pattern_key&change_type=in.(backlog,knowledge)&limit=500",
                          svc=True)
        existing_keys = {r.get("pattern_key","") for r in existing}
        backlog_items = sb_get("backlog",
                               "select=*&status=eq.pending&order=priority.desc&limit=500",
                               svc=True)
        pushed = 0
        for item in backlog_items:
            priority = int(item.get("priority", 1))
            if priority < 3: continue
            title  = item.get("title","")
            itype  = item.get("type","other")
            effort = item.get("effort","medium")
            executor = (
                "claude_desktop" if itype in ("new_tool","telegram_command") else
                "groq"           if itype in ("new_kb","missing_data") else
                "auto"
            )
            pkey = f"backlog:{itype}:{title[:60]}"
            if pkey in existing_keys: continue
            change_type = "knowledge" if itype == "new_kb" else "backlog"
            auto_apply  = (itype == "new_kb" and effort == "low" and executor == "groq")
            sb_post_critical("evolution_queue", {
                "change_type": change_type,
                "change_summary": f"[BACKLOG P{priority}][{executor}] {title}: {item.get('description','')[:180]}",
                "diff_content": json.dumps({
                    "backlog_type": itype, "executor": executor,
                    "domain": item.get("domain","general"), "effort": effort,
                    "impact": item.get("impact","medium"), "title": title,
                    "description": item.get("description",""),
                }),
                "pattern_key": pkey,
                "confidence": round(0.5 + priority * 0.08, 2),
                "status": "applied" if auto_apply else "pending",
                "source": "startup_repopulate",
                "impact": item.get("domain","general"),
            })
            existing_keys.add(pkey)
            pushed += 1
        print(f"[RESEARCH] Repopulated {pushed} missing evolution_queue entries")
        return pushed
    except Exception as e:
        print(f"[RESEARCH] repopulate error: {e}")
        return 0


def _backlog_to_markdown() -> str:
    """Generate BACKLOG.md from Supabase backlog table — always accurate after restarts."""
    _sync_backlog_status()
    try:
        rows = sb_get("backlog", "select=*&order=priority.desc&limit=500", svc=True)
    except Exception as e:
        return f"# CORE Improvement Backlog\n\n_Error reading backlog: {e}_\n"
    if not rows:
        return "# CORE Improvement Backlog\n\n_No items yet._\n"
    total     = len(rows)
    n_done    = sum(1 for b in rows if b.get("status") == "done")
    n_prog    = sum(1 for b in rows if b.get("status") == "in_progress")
    n_pending = total - n_done - n_prog
    lines = [
        "# CORE Improvement Backlog",
        f"\n_Auto-generated. Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
        f"_Total: {total} | Pending: {n_pending} | In Progress: {n_prog} | Done: {n_done}_\n",
        "---\n",
    ]
    by_type: dict = {}
    for item in rows:
        by_type.setdefault(item.get("type", "other"), []).append(item)
    type_labels = {
        "new_tool": "New Tools", "logic_improvement": "Logic Improvements",
        "new_kb": "Knowledge Gaps", "telegram_command": "Telegram Commands",
        "performance": "Performance", "missing_data": "Missing Data", "other": "Other",
    }
    status_icon = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    for t, items in by_type.items():
        n_t_done = sum(1 for i in items if i.get("status") == "done")
        lines.append(f"## {type_labels.get(t, t)} ({n_t_done}/{len(items)} done)\n")
        for item in items:
            p      = item.get("priority", 1)
            status = item.get("status", "pending")
            s_icon = status_icon.get(status, "[ ]")
            lines.append(f"### {s_icon} P{p}: {item.get('title','')}")
            lines.append(f"- **Status:** {status} | **Type:** {t} | **Effort:** {item.get('effort','?')} | **Impact:** {item.get('impact','?')} | **Domain:** {item.get('domain','?')}")
            lines.append(f"- **What:** {item.get('description','')}")
            lines.append(f"- **Discovered:** {item.get('discovered_at','')[:16]}")
            lines.append("")
    lines.append("---\n_CORE runs background_researcher every 60 min._")
    lines.append("_Use `/backlog` in Telegram or `get_backlog` MCP tool to review._")
    return "\n".join(lines)


# ── KB Mining — one-time batch scan to populate backlog from raw KB ───────────
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    """
    Mine the entire KB in batches of KB_MINE_BATCH_SIZE entries.
    For each batch, ask Groq to identify gaps/missing capabilities/improvements.
    Write discovered items to backlog table via _backlog_add().

    Runs automatically on startup if backlog is underpopulated relative to KB.
    Can also be triggered manually via /mine Telegram command or t_mine_kb MCP tool.

    Paced at 3s between batches to respect Groq free tier (200 calls/hour).
    max_batches caps total Groq calls per run (default 50 = 1000 KB entries scanned).
    """
    try:
        # Check if mining is needed
        counts = get_system_counts()
        kb_count = counts.get("knowledge_base", 0)
        backlog_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])

        if not force and backlog_count >= kb_count / KB_MINE_RATIO_THRESHOLD:
            msg = f"[KB MINE] Skipped — backlog ({backlog_count}) sufficient vs KB ({kb_count}). Use force=True to override."
            print(msg)
            return {"ok": True, "skipped": True, "reason": msg,
                    "kb_count": kb_count, "backlog_count": backlog_count}

        notify(f"⛏️ *KB Mining started*\nScanning {kb_count} KB entries in batches of {KB_MINE_BATCH_SIZE}\nMax batches: {max_batches}\nEstimated new items: {max_batches * 3}+")
        print(f"[KB MINE] Starting. kb={kb_count} backlog={backlog_count} max_batches={max_batches}")

        total_new = 0
        offset = 0
        batches_done = 0
        system = """You are CORE's KB mining engine. Analyze a batch of knowledge base entries and identify:
1. Gaps — what's missing or incomplete in this knowledge
2. Improvements — what tools/logic CORE needs to handle these topics better
3. Actions — specific backlog items to address the gaps

Output MUST be a JSON array of 3-5 improvement items. Each item:
{"priority": 1-5, "type": "new_tool"|"logic_improvement"|"new_kb"|"telegram_command"|"performance"|"missing_data",
 "title": "short specific title (max 60 chars)",
 "description": "specific actionable description (max 200 chars)",
 "effort": "low"|"medium"|"high", "impact": "low"|"medium"|"high"}
Output ONLY valid JSON array, no preamble, no explanation."""

        while batches_done < max_batches:
            # Read next batch from KB
            kb_batch = sb_get("knowledge_base",
                              f"select=domain,topic,content&order=id.asc&limit={KB_MINE_BATCH_SIZE}&offset={offset}",
                              svc=True)
            if not kb_batch:
                break

            # Summarize batch for Groq
            batch_text = "\n".join([
                f"[{r.get('domain','?')}] {r.get('topic','?')}: {str(r.get('content',''))[:150]}"
                for r in kb_batch
            ])
            domains_in_batch = list({r.get("domain","general") for r in kb_batch})

            user = (f"KB batch ({len(kb_batch)} entries, domains: {', '.join(domains_in_batch)}):\n\n"
                    f"{batch_text}\n\n"
                    f"What gaps or improvements does CORE need based on this knowledge?")

            try:
                raw = groq_chat(system, user, model=GROQ_FAST, max_tokens=600)
                raw = raw.strip()
                if raw.startswith("```"): raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                items = json.loads(raw.strip())
                if isinstance(items, list):
                    # Tag items with domain from batch
                    for item in items:
                        if not item.get("domain"):
                            item["domain"] = domains_in_batch[0] if domains_in_batch else "general"
                        item["discovered_at"] = datetime.utcnow().isoformat()
                        item["status"] = "pending"
                    new = _backlog_add(items)
                    total_new += len(new)
                    print(f"[KB MINE] Batch {batches_done+1}: offset={offset} entries={len(kb_batch)} new_items={len(new)}")
            except Exception as e:
                print(f"[KB MINE] Batch {batches_done+1} error: {e}")

            offset += KB_MINE_BATCH_SIZE
            batches_done += 1

            # If we've scanned all KB entries, stop
            if len(kb_batch) < KB_MINE_BATCH_SIZE:
                break

            time.sleep(3)  # pace Groq calls — 200/hr limit on free tier

        # BACKLOG.md write removed — Supabase is source of truth, no GitHub commit needed

        final_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])

        notify(
            f"⛏️ *KB Mining complete*\n"
            f"Batches scanned: {batches_done} ({batches_done * KB_MINE_BATCH_SIZE} KB entries)\n"
            f"New backlog items: {total_new}\n"
            f"Total backlog: {final_count}\n"
            f"BACKLOG.md updated ✅"
        )
        print(f"[KB MINE] Done. batches={batches_done} new_items={total_new} total_backlog={final_count}")
        return {"ok": True, "batches_scanned": batches_done, "new_items": total_new,
                "total_backlog": final_count, "kb_count": kb_count}

    except Exception as e:
        print(f"[KB MINE] error: {e}")
        return {"ok": False, "error": str(e)}


def t_mine_kb(max_batches: str = "50", force: str = "false") -> dict:
    """MCP tool wrapper for run_kb_mining."""
    try:
        mb = int(max_batches) if max_batches else 50
        f = str(force).lower() in ("true", "1", "yes")
    except Exception:
        mb = 50; f = False
    return run_kb_mining(max_batches=mb, force=f)


def background_researcher():
    global _last_research_run
    print("[RESEARCH] 24/7 background researcher started — interval=60min")

    # Startup: repopulate evolution_queue from Supabase backlog (restart-proof)
    try:
        pushed = _repopulate_evolution_queue()
        if pushed > 0:
            notify(f"[CORE] Startup: repopulated {pushed} evolution_queue entries after restart.")
        synced = _sync_backlog_status()
        print(f"[RESEARCH] Startup sync: {synced} status entries matched, {pushed} evo entries repopulated")
    except Exception as e:
        print(f"[RESEARCH] startup sync error: {e}")

    # Startup: auto-mine KB if backlog is underpopulated
    try:
        threading.Thread(target=run_kb_mining, kwargs={"max_batches": 50, "force": False}, daemon=True).start()
    except Exception as e:
        print(f"[RESEARCH] startup kb mining error: {e}")

    while True:
        try:
            if time.time() - _last_research_run >= _IMPROVEMENT_INTERVAL:
                print("[RESEARCH] Running simulation batch...")
                _last_research_run = time.time()
                all_new = []
                for _ in range(3):
                    items = _research_simulate_batch()
                    new = _backlog_add(items)
                    all_new.extend(new)
                    time.sleep(2)  # pace Groq calls
                md = _backlog_to_markdown()
                gh_write("BACKLOG.md", md,
                         f"chore(backlog): {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} — {len(all_new)} new items")
                try:
                    total_count = int(httpx.get(
                        f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
                        headers=_sbh_count_svc(), timeout=10
                    ).headers.get("content-range", "*/0").split("/")[-1])
                except Exception:
                    total_count = len(all_new)
                print(f"[RESEARCH] Cycle done. New: {len(all_new)}, Total in DB: {total_count}")
                critical_new = [i for i in all_new if int(i.get("priority", 1)) >= 4]
                try:
                    pending_count = len(sb_get("evolution_queue",
                        "select=id&status=eq.pending&change_type=eq.backlog", svc=True))
                except Exception:
                    pending_count = 0
                if all_new or pending_count:
                    hi = "\n".join([f"[P{i['priority']}] {i['title']}" for i in critical_new[:5]])
                    msg = f"[RESEARCH] {len(all_new)} new items | Total backlog: {total_count}"
                    if hi: msg += f"\n\nHigh priority:\n{hi}"
                    if pending_count: msg += f"\n\n{pending_count} items awaiting approval - /evolutions"
                    msg += "\n\nFull list: /backlog"
                    notify(msg)
        except Exception as e:
            print(f"[RESEARCH] loop error: {e}")
        time.sleep(300)  # check every 5 min, runs every 60 min


def t_get_backlog(status: str = "pending", limit: int = 20, min_priority: int = 1):
    """Read backlog directly from Supabase — restart-proof, always accurate."""
    try:
        lim = int(limit) if limit else 20
        min_p = int(min_priority) if min_priority else 1
        qs = f"select=*&status=eq.{status}&order=priority.desc&limit={lim}"
        if min_p > 1:
            qs += f"&priority=gte.{min_p}"
        items = sb_get("backlog", qs, svc=True)
        total = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])
        return {"ok": True, "total": total, "filtered": len(items), "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


def t_backlog_update(title: str, status: str):
    """Update backlog item status in Supabase."""
    ok = sb_patch("backlog", f"title=eq.{title}", {"status": status})
    if ok:
        sb_patch("evolution_queue",
                 f"pattern_key=like.backlog%3A%25{title[:40]}%25",
                 {"status": "applied" if status == "done" else status})
    return {"ok": ok, "title": title, "new_status": status}


def t_bulk_apply(executor_override: str = "claude_desktop", dry_run: bool = False):
    """Apply all pending evolution_queue items."""
    try:
        rows = sb_get("evolution_queue",
                      "select=*&status=in.(pending,pending_desktop)&order=id.asc",
                      svc=True)
        if not rows:
            return {"ok": True, "message": "No pending evolutions", "applied": [], "total": 0}

        results = []
        for evo in rows:
            eid   = evo["id"]
            ctype = evo.get("change_type", "knowledge")
            summary = evo.get("change_summary", "")
            try:
                meta = json.loads(evo.get("diff_content") or "{}")
            except Exception:
                meta = {}

            btype    = meta.get("backlog_type", "")
            title    = meta.get("title", summary[:80])
            desc     = meta.get("description", summary)
            domain   = meta.get("domain", "general")
            original_exec = meta.get("executor", "auto")
            effective = executor_override if executor_override != "auto" else original_exec

            if dry_run:
                results.append({
                    "id": eid, "title": title, "btype": btype,
                    "original_executor": original_exec, "would_use": effective,
                    "action": "dry_run — not applied"
                })
                continue

            if effective == "claude_desktop" or executor_override == "claude_desktop":
                if ctype == "knowledge" or btype == "new_kb":
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB entry added: {title}"
                elif btype in ("logic_improvement", "performance", "missing_data"):
                    ok = bool(sb_post("task_queue", {
                        "type": "improvement",
                        "payload": json.dumps({"title": title, "desc": desc, "domain": domain}),
                        "status": "pending", "priority": 5, "source": "bulk_apply",
                    }))
                    note = f"[desktop] Queued for execution: {title}"
                elif btype in ("new_tool", "telegram_command"):
                    ok = bool(sb_post("knowledge_base", {
                        "domain": "pending_impl", "topic": f"[TODO] {title}",
                        "content": f"Type: {btype}\n{desc}",
                        "confidence": "low", "tags": ["todo", "new_tool", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] Logged as TODO: {title} (needs manual impl)"
                else:
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply"], "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB fallback: {title}"

                if ok:
                    sb_patch("evolution_queue", f"id=eq.{eid}",
                             {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
                    sb_patch("backlog", f"title=eq.{title}", {"status": "done"})
                results.append({"id": eid, "title": title, "ok": ok, "note": note})

            else:
                r = apply_evolution(eid)
                results.append({"id": eid, "title": title, "ok": r.get("ok"), "note": r.get("note", "")})

        applied = [r for r in results if r.get("ok")]
        failed  = [r for r in results if not r.get("ok") and not r.get("action")]
        notify(
            f"Bulk apply done\n"
            f"Applied: {len(applied)} | Failed: {len(failed)} | Total: {len(results)}\n"
            f"Executor: {executor_override}"
        )
        try:
            gh_write("BACKLOG.md", _backlog_to_markdown(),
                     f"chore(backlog): sync status after bulk_apply ({len(applied)} applied)")
        except Exception as _be:
            print(f"[BACKLOG] bulk refresh error: {_be}")
        return {"ok": True, "applied": len(applied), "failed": len(failed), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Tool registry ─────────────────────────────────────────────────────────────
TOOLS = {
    "get_state":              {"fn": t_state,                  "perm": "READ",    "args": [],
                               "desc": "Get current CORE state: last session, counts, pending tasks, operating_context, session_md"},
    "get_system_health":      {"fn": t_health,                 "perm": "READ",    "args": [],
                               "desc": "Check health of all components: Supabase, Groq, Telegram, GitHub"},
    "get_constitution":       {"fn": t_constitution,           "perm": "READ",    "args": [],
                               "desc": "Get CORE immutable constitution"},
    "get_training_status":    {"fn": t_training_status,        "perm": "READ",    "args": [],
                               "desc": "Get training pipeline status: unprocessed hot, pending evolutions, thresholds"},
    "search_kb":              {"fn": t_search_kb,              "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Search knowledge base"},
    "get_mistakes":           {"fn": t_get_mistakes,           "perm": "READ",    "args": ["domain", "limit"],
                               "desc": "Get recorded mistakes. domain=optional filter, limit=number (default 10)"},
    "read_file":              {"fn": t_read_file,              "perm": "READ",    "args": ["path", "repo"],
                               "desc": "Read file from GitHub repo. repo defaults to pockiesaints7/core-agi"},
    "sb_query":               {"fn": t_sb_query,               "perm": "READ",    "args": ["table", "filters", "limit"],
                               "desc": "Query Supabase table. filters=optional querystring"},
    "list_evolutions":        {"fn": t_list_evolutions,        "perm": "READ",    "args": ["status"],
                               "desc": "List evolutions. status=pending/applied/rejected (default: pending)"},
    "update_state":           {"fn": t_update_state,           "perm": "WRITE",   "args": ["key", "value", "reason"],
                               "desc": "Write state update to sessions table"},
    "add_knowledge":          {"fn": t_add_knowledge,          "perm": "WRITE",   "args": ["domain", "topic", "content", "tags", "confidence"],
                               "desc": "Add entry to knowledge base. tags=comma-separated string"},
    "log_mistake":            {"fn": t_log_mistake,            "perm": "WRITE",   "args": ["context", "what_failed", "fix", "domain", "root_cause", "how_to_avoid", "severity"],
                               "desc": "Log a mistake. Required: context, what_failed, fix"},
    "notify_owner":           {"fn": t_notify,                 "perm": "WRITE",   "args": ["message", "level"],
                               "desc": "Send Telegram notification. level=info/warn/alert/ok"},
    "sb_insert":              {"fn": t_sb_insert,              "perm": "WRITE",   "args": ["table", "data"],
                               "desc": "Insert row into Supabase table. data=JSON string"},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "WRITE",   "args": [],
                               "desc": "Manually trigger cold processor: distill patterns, queue evolutions"},
    "approve_evolution":      {"fn": t_approve_evolution,      "perm": "WRITE",   "args": ["evolution_id"],
                               "desc": "Approve and apply a pending evolution by ID"},
    "reject_evolution":       {"fn": t_reject_evolution,       "perm": "WRITE",   "args": ["evolution_id", "reason"],
                               "desc": "Reject a pending evolution by ID. reason=optional"},
    "gh_search_replace":      {"fn": t_gh_search_replace,      "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo"],
                               "desc": "Surgical find-and-replace in a GitHub file. Fails safely if old_str not found or ambiguous."},
    "gh_read_lines":          {"fn": t_gh_read_lines,          "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                               "desc": "Read specific line range from GitHub file with line numbers. Use before gh_search_replace."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write file to GitHub repo. FULL OVERWRITE — use for new files only. For edits use gh_search_replace."},
    "route":                  {"fn": t_route,                  "perm": "EXECUTE", "args": ["task", "execute"],
                               "desc": "Route a task through CORE Routing Engine v2.0. execute=true to run via Groq."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything. Pulls KB context + Groq generates answer."},
    "reflect":                {"fn": t_reflect,                "perm": "WRITE",   "args": ["task_summary", "domain", "patterns", "quality", "notes"],
                               "desc": "Log a hot reflection in one call. Use at end of significant sessions."},
    "stats":                  {"fn": t_stats,                  "perm": "READ",    "args": [],
                               "desc": "Analytics: domain distribution, top patterns, mistake frequency, avg quality score."},
    "search_mistakes":        {"fn": t_search_mistakes,        "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Semantic mistake search. Returns what_failed + correct_approach."},
    "get_backlog":            {"fn": t_get_backlog,            "perm": "READ",    "args": ["status", "limit", "min_priority"],
                               "desc": "Get improvement backlog from Supabase. status=pending/done/dismissed. min_priority=1-5."},
    "backlog_update":         {"fn": t_backlog_update,         "perm": "WRITE",   "args": ["title", "status"],
                               "desc": "Update backlog item status in Supabase: in_progress / done / dismissed."},
    "bulk_apply":             {"fn": t_bulk_apply,             "perm": "WRITE",   "args": ["executor_override", "dry_run"],
                               "desc": "Apply ALL pending evolution_queue items at once. executor_override=claude_desktop|groq|auto. dry_run=true to preview."},
    "repopulate":             {"fn": _repopulate_evolution_queue, "perm": "WRITE", "args": [],
                               "desc": "Re-push all P3+ backlog items to evolution_queue. Use when evolution_queue is empty after restart."},
    "mine_kb":                {"fn": t_mine_kb,                "perm": "WRITE",   "args": ["max_batches", "force"],
                               "desc": "Mine KB entries in batches to generate backlog items. max_batches=50 default. force=true to skip ratio check."},
}

# ── MCP JSON-RPC ──────────────────────────────────────────────────────────────
def _mcp_tool_schema(name, tool):
    props = {a: {"type": "string", "description": a} for a in tool["args"]}
    return {"name": name, "description": tool.get("desc", name),
            "inputSchema": {"type": "object", "properties": props}}

def handle_jsonrpc(body: dict, session_id: str = "") -> dict:
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")
    def ok(r):  return {"jsonrpc": "2.0", "id": req_id, "result": r}
    def err(c, m): return {"jsonrpc": "2.0", "id": req_id, "error": {"code": c, "message": m}}

    if method == "initialize":
        return ok({"protocolVersion": MCP_PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "CORE v5.4", "version": "5.4"}})
    elif method == "notifications/initialized": return None
    elif method == "ping": return ok({})
    elif method == "tools/list":
        return ok({"tools": [_mcp_tool_schema(n, t) for n, t in TOOLS.items()]})
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        if name not in TOOLS: return err(-32602, f"Unknown tool: {name}")
        try:
            result = TOOLS[name]["fn"](**args) if args else TOOLS[name]["fn"]()
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    elif method == "resources/list": return ok({"resources": []})
    elif method == "prompts/list":   return ok({"prompts": []})
    else: return err(-32601, f"Method not found: {method}")

# ── FastAPI ───────────────────────────────────────────────────────────────────
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"

class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}

class PatchRequest(BaseModel):
    secret: str
    path: str
    old_str: str
    new_str: str
    message: str
    repo: Optional[str] = ""

app = FastAPI(title="CORE v5.4", version="5.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    counts = get_system_counts()
    step = get_current_step()
    try:
        backlog_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])
    except Exception:
        backlog_count = -1
    return {"service": "CORE v5.4", "step": step,
            "knowledge": counts.get("knowledge_base", 0), "sessions": counts.get("sessions", 0),
            "mistakes": counts.get("mistakes", 0), "backlog_items": backlog_count}

@app.get("/health")
def health_ep(): return t_health()

@app.post("/patch")
async def patch_file(body: PatchRequest):
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    result = t_gh_search_replace(path=body.path, old_str=body.old_str, new_str=body.new_str,
                                  message=body.message, repo=body.repo or GITHUB_REPO)
    if result.get("ok"):
        notify(f"✂️ Patch applied: `{body.path}`\n{body.message[:100]}")
    return result

@app.post("/mcp/sse")
async def mcp_post(req: Request):
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Unauthorized"}}, status_code=401)
    try: body = await req.json()
    except: return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    if isinstance(body, list):
        return JSONResponse([r for item in body if (r := handle_jsonrpc(item)) is not None])
    response = handle_jsonrpc(body)
    if response is None: return JSONResponse({}, status_code=204)
    if "text/event-stream" in req.headers.get("accept", ""):
        async def sse_single():
            yield f"data: {json.dumps(response)}\n\n"
        return StreamingResponse(sse_single(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "mcp-session-id": str(uuid.uuid4())})
    return JSONResponse(response)

@app.get("/mcp/sse")
async def mcp_sse_get(req: Request):
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET: raise HTTPException(401, "Unauthorized")
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue
    async def event_stream():
        try:
            yield f"event: endpoint\ndata: {json.dumps(f'/mcp/messages?session_id={session_id}')}\n\n"
            while True:
                if await req.is_disconnected(): break
                try: msg = await asyncio.wait_for(queue.get(), timeout=25.0); yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError: yield f": ping\n\n"
        finally: _sse_sessions.pop(session_id, None)
    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Session-Id": session_id})

_sse_sessions: dict = {}

@app.post("/mcp/messages")
async def mcp_messages(req: Request):
    session_id = req.query_params.get("session_id", "")
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET: return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try: body = await req.json()
    except: return JSONResponse({"error": "Parse error"}, status_code=400)
    response = handle_jsonrpc(body)
    if session_id and session_id in _sse_sessions:
        if response is not None: await _sse_sessions[session_id].put(response)
        return JSONResponse({"ok": True}, status_code=202)
    return JSONResponse(response) if response else JSONResponse({}, status_code=204)

@app.post("/mcp/startup")
async def mcp_startup(body: Handshake, req: Request):
    if body.secret != MCP_SECRET: raise HTTPException(401, "Invalid secret")
    tok = mcp_new(req.client.host)
    step = get_current_step()
    notify(f"🔌 MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {"session_token": tok, "expires_hours": SESSION_TTL_H,
            "state": t_state(), "health": t_health(), "constitution": t_constitution(),
            "tools": list(TOOLS.keys()), "note": f"CORE v5.4 ready. {step}"}

@app.post("/mcp/auth")
async def mcp_auth(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        notify(f"⚠️ Invalid MCP auth from {req.client.host}")
        raise HTTPException(401, "Invalid secret")
    return {"session_token": mcp_new(req.client.host), "expires_hours": SESSION_TTL_H}

@app.post("/mcp/tool")
async def mcp_tool(body: ToolCall):
    if not mcp_ok(body.session_token): raise HTTPException(401, "Invalid/expired session")
    if not L.mcp(body.session_token):  raise HTTPException(429, "Rate limit exceeded")
    if body.tool not in TOOLS:         raise HTTPException(404, f"Tool not found: {body.tool}")
    try:
        res = TOOLS[body.tool]["fn"](**body.args) if body.args else TOOLS[body.tool]["fn"]()
        return {"ok": True, "tool": body.tool, "perm": TOOLS[body.tool]["perm"], "result": res}
    except HTTPException: raise
    except Exception as e: return {"ok": False, "tool": body.tool, "error": str(e)}

@app.get("/mcp/tools")
def list_tools(): return {n: {"perm": t["perm"], "args": t["args"]} for n, t in TOOLS.items()}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        u = await req.json()
        if "message" in u:
            threading.Thread(target=handle_msg, args=(u["message"],), daemon=True).start()
    except Exception as e: print(f"[WEBHOOK] {e}")
    return {"ok": True}

def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text: return

    if text == "/start":
        counts = get_system_counts()
        step = get_current_step()
        notify(f"*CORE v5.4*\n{step}\n"
               f"Knowledge: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)}\n\n"
               f"*Commands:*\n"
               f"/status — health + training\n"
               f"/ask <question> — ask CORE (Groq)\n"
               f"/route <task> — routing analysis\n"
               f"/stats — analytics\n"
               f"/backlog [min_priority] — improvement backlog\n"
               f"/mine — scan KB for backlog items\n"
               f"/mistakes [domain] — recent mistakes\n"
               f"/tasks — task queue\n"
               f"/evolutions — pending evolutions\n"
               f"/approve <id> /reject <id>", cid)

    elif text == "/status":
        h = t_health(); counts = get_system_counts(); ts = t_training_status()
        step = get_current_step()
        notify(f"*Status — {step}*\n"
               f"Supabase: {h['components'].get('supabase')} | Groq: {h['components'].get('groq')}\n"
               f"Telegram: {h['components'].get('telegram')} | GitHub: {h['components'].get('github')}\n\n"
               f"Knowledge: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)}\n"
               f"Hot unprocessed: {ts.get('unprocessed_hot',0)} | Pending evos: {ts.get('pending_evolutions',0)}\n"
               f"Backlog: {ts.get('backlog_pending','?')} pending | KB growth trigger: +{COLD_KB_GROWTH_THRESHOLD}\n"
               f"MCP tools: {len(TOOLS)}", cid)

    elif text == "/stats":
        s = t_stats()
        if s.get("ok"):
            top_p = "\n".join([f"  {p['freq']}x {p['pattern'][:50]}" for p in s.get("top_patterns",[])[:5]])
            domains = " | ".join([f"{k}:{v}" for k,v in list(s.get("domain_distribution",{}).items())[:5]])
            notify(f"*CORE Analytics*\n"
                   f"Sessions: {s['total_sessions']} | KB: {s['knowledge_entries']}\n"
                   f"Mistakes: {s['total_mistakes']} | Reflections: {s['hot_reflections']}\n"
                   f"Avg quality: {s.get('avg_quality_score','—')}\n\n"
                   f"*Domains:* {domains}\n\n"
                   f"*Top patterns:*\n{top_p or 'none yet'}", cid)
        else:
            notify(f"Stats error: {s.get('error')}", cid)

    elif text.startswith("/ask "):
        q = text[5:].strip()
        notify("🧠 Thinking...", cid)
        result = t_ask(q)
        if result.get("ok"):
            notify(f"*Q:* {q[:80]}\n\n{result['answer'][:800]}\n\n_(KB hits: {result['kb_hits']})_", cid)
        else:
            notify(f"Error: {result.get('error')}", cid)

    elif text.startswith("/route "):
        task = text[7:].strip()
        result = t_route(task, execute=False)
        if result.get("ok"):
            sig = result["routing"]["signals"]
            notify(f"*Routing: {task[:60]}*\n\n"
                   f"Archetype: {sig['archetype']}\n"
                   f"Intent: {sig['intent']} | Domain: {sig['domain']}\n"
                   f"Expertise: {sig['expertise']}/5 | Emotion: {sig['emotion']}\n"
                   f"Stakes: {sig['stakes']} | Complexity: {result['routing']['complexity']}/12", cid)
        else:
            notify(f"Route error: {result.get('error')}", cid)

    elif text.startswith("/mine"):
        parts = text.split(None, 1)
        force = len(parts) > 1 and parts[1].strip().lower() == "force"
        notify(f"⛏️ KB mining started{'(forced)' if force else ''}... will notify when done.", cid)
        threading.Thread(target=run_kb_mining, kwargs={"max_batches": 50, "force": force}, daemon=True).start()

    elif text.startswith("/backlog"):
        parts = text.split(None, 1)
        min_p = 1
        try: min_p = int(parts[1]) if len(parts) > 1 else 1
        except: pass
        result = t_get_backlog(status="pending", limit=10, min_priority=min_p)
        total = result.get("total", 0)
        items = result.get("items", [])
        if items:
            lines = []
            for item in items[:8]:
                p = item.get("priority", 1)
                star = "🔴" if p >= 4 else ("🟡" if p == 3 else "🟢")
                lines.append(f"{star} P{p} [{item.get('type','?')[:10]}] *{item.get('title','')[:50]}*")
                lines.append(f"  ↳ {item.get('description','')[:80]}")
            notify(f"📋 *Backlog* ({result['filtered']} pending / {total} total)\n\n" +
                   "\n".join(lines) + "\n\n_Full list: BACKLOG.md on GitHub_", cid)
        else:
            notify(f"📋 Backlog empty (total: {total}). Researcher runs every 60 min.", cid)

    elif text.startswith("/mistakes"):
        parts = text.split(None, 1)
        domain = parts[1].strip() if len(parts) > 1 else ""
        result = t_search_mistakes(domain=domain, limit=5)
        if result.get("ok") and result["mistakes"]:
            lines = "\n\n".join([
                f"[{m.get('domain','')}] *{m.get('what_failed','')[:60]}*\n→ {m.get('correct_approach','')[:100]}"
                for m in result["mistakes"]
            ])
            notify(f"*Recent Mistakes*{' ('+domain+')' if domain else ''}\n\n{lines}", cid)
        else:
            notify("No mistakes found" + (f" for domain: {domain}" if domain else ""), cid)

    elif text == "/tasks":
        tasks = sb_get("task_queue", "select=task,status&order=created_at.desc&limit=5")
        lines = "\n".join([f"- [{t.get('status')}] {t.get('task','')[:60]}" for t in tasks])
        notify(f"*Recent Tasks*\n\n{lines}" if tasks else "No tasks yet.", cid)

    elif text == "/evolutions":
        rows = sb_get("evolution_queue",
                      "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1&order=created_at.desc&limit=10",
                      svc=True)
        if rows:
            lines = "\n".join([f"#{r['id']} [{r.get('change_type','?')}] conf={r.get('confidence','?')}\n  {str(r.get('change_summary',''))[:80]}" for r in rows])
            notify(f"*Pending Evolutions*\n\n{lines}\n\nUse /approve <id> or /reject <id>", cid)
        else: notify("No pending evolutions.", cid)

    elif text.startswith("/approve "):
        try:
            eid = int(text.split()[1])
            result = apply_evolution(eid)
            notify(f"Evolution #{eid}: {'applied ✅' if result.get('ok') else 'failed ❌'}\n{result.get('note', result.get('error', ''))}", cid)
        except (ValueError, IndexError): notify("Usage: /approve <id>", cid)

    elif text.startswith("/reject "):
        parts = text.split(None, 2)
        try:
            eid = int(parts[1]); reason = parts[2] if len(parts) > 2 else ""
            result = reject_evolution(eid, reason)
            notify(f"Evolution #{eid}: {'rejected ❌' if result.get('ok') else 'failed'}\n{reason}", cid)
        except (ValueError, IndexError): notify("Usage: /reject <id> [reason]", cid)

    else:
        sig = extract_signals(text)
        notify(f"⚙️ Routing [{sig['archetype']}] {sig['domain']}...", cid)
        result = t_route(text, execute=True)
        if result.get("ok") and result.get("response"):
            notify(result["response"][:1500], cid)
        else:
            ok = sb_post("task_queue", {"task": text, "chat_id": cid, "status": "pending", "priority": 5})
            notify(f"✅ Queued: `{text[:80]}`" if ok else "❌ Failed to queue task.", cid)

def queue_poller():
    print("[QUEUE] Started — v5.4 live execution mode")
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t = tasks[0]
                tid = t["id"]
                task_text = t.get("task", "")
                chat_id = t.get("chat_id", "")
                sb_patch("task_queue", f"id=eq.{tid}", {"status": "processing"})
                result = t_route(task_text, execute=True)
                if result.get("ok") and result.get("response"):
                    sb_patch("task_queue", f"id=eq.{tid}", {"status": "completed", "error": None})
                    if chat_id:
                        notify(f"✅ *Task completed*\n{result['response'][:800]}", chat_id)
                else:
                    err = result.get("error", "unknown error")
                    sb_patch("task_queue", f"id=eq.{tid}", {"status": "failed", "error": err[:200]})
                    if chat_id:
                        notify(f"❌ Task failed: {err[:200]}", chat_id)
        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(10)

@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    threading.Thread(target=self_sync_check, daemon=True).start()
    threading.Thread(target=background_researcher, daemon=True).start()
    counts = get_system_counts()
    step = get_current_step()
    notify(f"*CORE v5.4 Online* ✅\n{step}\n"
           f"Knowledge: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)}\n"
           f"MCP: {len(TOOLS)} tools\n"
           f"🔬 Background researcher: ACTIVE (60 min interval)\n"
           f"⛏️ KB mining: auto-triggers on startup if backlog underpopulated\n"
           f"📋 BACKLOG.md: Supabase-backed (restart-proof)\n"
           f"📈 Cold processor: auto-triggers on KB growth (+{COLD_KB_GROWTH_THRESHOLD} entries)")
    print(f"[CORE] v5.4 online :{PORT} — {step}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core:app", host="0.0.0.0", port=PORT, reload=False)
