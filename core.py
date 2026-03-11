"""CORE v5.0 — Recursive Self-Improvement Architecture
Owner: REINVAGNAR
Step status is dynamic — always read from SESSION.md on GitHub.
Do NOT hardcode step numbers anywhere in this file.

Fix log:
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
PATTERN_EVO_THRESHOLD     = 3
KNOWLEDGE_AUTO_CONFIDENCE = 0.7

# Self-sync config
CORE_SELF_STALE_DAYS = 7   # warn if CORE_SELF.md not updated in 7 days while sessions active

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
    """Rate-limited insert. Returns False silently if rate limit hit."""
    if not L.sbw(): return False
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
    if not r.is_success:
        print(f"[SB POST] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

def sb_post_critical(t, d):
    """Critical insert: bypasses rate limiter. Use only for low-freq summary writes
    (e.g. cold_reflections, evolution_queue) where silent skip is unacceptable."""
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

# ── Dynamic step label — NEVER hardcode step numbers ─────────────────────────
_step_cache: dict = {"label": "unknown", "ts": 0.0}
_STEP_CACHE_TTL = 300  # seconds — refresh every 5 min

def get_current_step() -> str:
    """
    Read current step label dynamically from SESSION.md on GitHub.
    Cached for 5 minutes to avoid hammering GitHub on every request.
    NEVER hardcode a step label anywhere in this file — always call this function.
    """
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

# ── Self-sync check — V1/V2/V6 fix ───────────────────────────────────────────
def self_sync_check():
    """
    Fix 2026-03-11L: Detect stale CORE_SELF.md and warn owner via Telegram.
    Runs on startup and after every apply_evolution().
    Prevents CORE from drifting from self-knowledge over time.
    """
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
            notify("⚠️ *CORE Self-Sync Warning*\nCORE_SELF.md has no `Last updated:` date. Please verify it's current.")
            return {"ok": False, "reason": "no_date"}

        days_stale = (datetime.utcnow() - last_updated).days
        if days_stale > CORE_SELF_STALE_DAYS:
            # Check if there's been active sessions since last update
            recent = sb_get("sessions", f"select=id&order=created_at.desc&limit=1", svc=True)
            if recent:
                notify(
                    f"⚠️ *CORE Self-Sync Warning*\n"
                    f"CORE_SELF.md last updated *{days_stale} days ago*.\n"
                    f"Active sessions detected since then.\n"
                    f"Please review and update CORE_SELF.md if architecture has changed.\n"
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
    """
    Fix 2026-03-11L: For structural evolutions, remind owner to update CORE_SELF.md.
    Called inside apply_evolution() for schema/tool/architecture/file change types.
    """
    structural_types = {"schema", "tool", "architecture", "file", "behavior"}
    change_type = evo.get("change_type", "")
    diff_content = evo.get("diff_content", "") or ""

    if change_type in structural_types and "core_self_updated" not in diff_content.lower():
        notify(
            f"📋 *CORE Self-Sync Reminder*\n"
            f"Evolution #{evo.get('id')} applied (type: `{change_type}`).\n"
            f"This is a structural change — please update *CORE_SELF.md*:\n"
            f"• Schema section if tables changed\n"
            f"• MCP Tools section if tools changed\n"
            f"• Architecture section if infra changed\n"
            f"→ github.com/pockiesaints7/core-agi/blob/main/CORE_SELF.md"
        )
        print(f"[SELF_SYNC] Structural evolution #{evo.get('id')} — owner reminded to update CORE_SELF.md")

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

# ── Training pipeline ────────────────────────────────────────────────────────────

def auto_hot_reflection(session_data: dict):
    """Auto-generate a hot_reflection from a sessions row. Lightweight, no LLM."""
    try:
        summary   = session_data.get("summary", "")
        actions   = session_data.get("actions", []) or []
        interface = session_data.get("interface", "unknown")
        total     = max(len(actions), 1)
        verify_rate   = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["verify","readback","confirm"])) / total, 2)
        mistake_rate  = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["mistake","error","fix","wrong"])) / total, 2)
        domain = "general"
        for kw, d in [("supabase","db"),("github","code"),("telegram","bot"),("mcp","mcp"),("training","training"),("knowledge","kb")]:
            if kw in summary.lower(): domain = d; break
        ok = sb_post("hot_reflections", {
            "task_summary": summary[:300], "domain": domain,
            "verify_rate": verify_rate, "mistake_consult_rate": mistake_rate,
            "new_patterns": [], "new_mistakes": [],
            "quality_score": None, "gaps_identified": None,
            "reflection_text": f"Auto-generated from {interface} session. Actions: {total}.",
            "processed_by_cold": False,  # JSON body bool is fine — only querystring filter needs int
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


def run_cold_processor():
    """Batch: distill patterns, upsert pattern_frequency, queue evolutions, write summary.
    Fix (2026-03-11i): processed_by_cold filter uses eq.0/eq.1 (integer) not eq.false/eq.true.
    """
    try:
        # Fix 2026-03-11i: use eq.0 not eq.false — PostgREST rejects boolean in querystring
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
            existing = [e for e in sb_get("pattern_frequency",
                        f"select=id,frequency,auto_applied&pattern_key=eq.{key}&limit=1", svc=True)
                        if e.get("id") != 1]

            if existing:
                rec      = existing[0]
                new_freq = (rec.get("frequency") or 0) + batch_count
                sb_patch("pattern_frequency", f"id=eq.{rec['id']}",
                         {"frequency": new_freq, "last_seen": datetime.utcnow().isoformat()})
                print(f"[COLD] pattern '{key[:50]}' updated freq={new_freq}")
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
                        print(f"[COLD] evolution queued for '{key[:50]}'")
            else:
                new_freq = batch_count
                sb_upsert("pattern_frequency", {
                    "pattern_key": key, "domain": batch_domain.get(key, "general"),
                    "description": key, "frequency": new_freq, "confidence": 0.5,
                    "first_seen": datetime.utcnow().isoformat(),
                    "last_seen": datetime.utcnow().isoformat(), "auto_applied": False,
                }, "pattern_key")
                print(f"[COLD] new pattern '{key[:50]}' freq={new_freq}")
                if new_freq >= PATTERN_EVO_THRESHOLD:
                    if sb_post_critical("evolution_queue", {
                        "status": "pending", "change_type": "knowledge",
                        "change_summary": f"New pattern '{key}' seen {new_freq}x in first batch — promote to knowledge base",
                        "pattern_key": key, "frequency": new_freq,
                        "confidence": min(0.5 + new_freq * 0.1, 0.95),
                        "impact": "low", "reversible": True, "owner_notified": False,
                    }):
                        evolutions_queued += 1
                        print(f"[COLD] evolution queued for new pattern '{key[:50]}'")

        period_end = datetime.utcnow().isoformat()
        cold_ok = sb_post_critical("cold_reflections", {
            "period_start":      period_start,
            "period_end":        period_end,
            "hot_count":         len(hots),
            "patterns_found":    len(batch_counts),
            "evolutions_queued": evolutions_queued,
            "auto_applied":      0,
            "summary_text":      f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns. {evolutions_queued} evolutions queued.",
        })
        print(f"[COLD] cold_reflections insert ok={cold_ok}")

        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": True})

        if evolutions_queued > 0:
            notify(f"\u2728 Cold processor: {evolutions_queued} evolution(s) queued from {len(hots)} sessions.\nUse /evolutions to review.")

        print(f"[COLD] Done: processed={len(hots)} patterns={len(batch_counts)} evolutions={evolutions_queued}")
        return {"ok": True, "processed": len(hots), "patterns_found": len(batch_counts), "evolutions_queued": evolutions_queued}

    except Exception as e:
        print(f"[COLD] error: {e}")
        return {"ok": False, "error": str(e)}


def apply_evolution(evolution_id: int):
    """Apply an approved evolution (knowledge/code/behavior)."""
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
            note = f"Patch written to {fname} — review and merge manually"
        elif change_type == "behavior":
            if not diff_content: return {"ok": False, "error": "behavior evolution requires diff_content"}
            applied = gh_write("BEHAVIOR_UPDATES.md", diff_content,
                               f"Behavior evolution #{evolution_id}: {change_summary[:60]}")
            note = "Written to BEHAVIOR_UPDATES.md — review and merge into operating_context.json manually"

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            notify(f"\u2705 Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
            print(f"[EVO] Applied #{evolution_id} ({change_type})")
            # Fix 2026-03-11L: remind owner to update CORE_SELF.md for structural evolutions
            check_evolution_self_sync(evo)
        else:
            notify(f"\u274c Evolution #{evolution_id} apply failed\nType: {change_type}")
        return {"ok": applied, "evolution_id": evolution_id, "change_type": change_type, "note": note}
    except Exception as e:
        print(f"[EVO] error: {e}")
        return {"ok": False, "error": str(e)}


def reject_evolution(evolution_id: int, reason: str = ""):
    """Reject a pending evolution and log as mistake."""
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
        notify(f"\u274c Evolution #{evolution_id} rejected.\nReason: {reason or 'No reason given'}")
        return {"ok": True, "evolution_id": evolution_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_last_cold_run: float = 0.0

def cold_processor_loop():
    """Background thread: trigger cold processor every 10 unprocessed hots OR 24h."""
    global _last_cold_run
    print("[COLD] Background loop started")
    while True:
        try:
            # Fix 2026-03-11i: use eq.0 not eq.false in querystring
            hots        = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
            unprocessed = len(hots)
            time_since  = time.time() - _last_cold_run
            if unprocessed >= COLD_HOT_THRESHOLD or (time_since >= COLD_TIME_THRESHOLD and unprocessed > 0):
                print(f"[COLD] Triggering: unprocessed={unprocessed} time_since={int(time_since)}s")
                run_cold_processor()
                _last_cold_run = time.time()
            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    print(f"[EVO] Auto-applying #{evo['id']} confidence={conf}")
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
    icons = {"info": "\u2139\ufe0f", "warn": "\u26a0\ufe0f", "alert": "\U0001f6a8", "ok": "\u2705"}
    return {"ok": notify(f"{icons.get(level, '\u00bb')} CORE\n{message}")}

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
        # Fix 2026-03-11i: use eq.0 not eq.false
        unprocessed = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
        pending_evo = sb_get("evolution_queue", "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1", svc=True)
        return {"status": f"Training pipeline ACTIVE — {get_current_step()}",
                "unprocessed_hot": len(unprocessed), "pending_evolutions": len(pending_evo),
                "evolutions": pending_evo[:5], "cold_threshold": COLD_HOT_THRESHOLD,
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
    """
    Surgical find-and-replace in a GitHub file.
    Returns ok=False if old_str not found or ambiguous. Never corrupts file.
    """
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
        print(f"[GH] search_replace {'ok' if ok else 'fail'}: {path}")
        return {"ok": ok, "path": path, "replaced": old_str[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_gh_read_lines(path, start_line=1, end_line=50, repo=""):
    """
    Read specific line range from a GitHub file with line numbers.
    Use before gh_search_replace to find exact strings to patch.
    """
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


# ── Groq LLM call ────────────────────────────────────────────────────────────
def groq_chat(system: str, user: str, model: str = None, max_tokens: int = 1024) -> str:
    """Single Groq chat completion. Returns text or raises."""
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


# ── Signal extraction (v2.0 routing pre-layer) ────────────────────────────────
def extract_signals(task: str) -> dict:
    """
    Extract 5 routing signals from raw task text.
    S1=intent, S2=domain, S3=expertise(1-5), S4=emotion, S5=scope/stakes
    Fast path: keyword-based, no LLM needed.
    """
    t = task.lower()

    # S1: Intent verb
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

    # S2: Domain fingerprint
    domain = "general"
    for kw, d in [("def ","code"),("function","code"),("import ","code"),("class ","code"),("sql","code"),
                  ("contract","legal"),("liability","legal"),("clause","legal"),("lawsuit","legal"),
                  ("invoice","finance"),("revenue","finance"),("cash flow","finance"),("tax","finance"),
                  ("patient","medical"),("symptoms","medical"),("diagnosis","medical"),("medication","medical"),
                  ("marketing","business"),("customers","business"),("startup","business"),("sales","business"),
                  ("essay","academic"),("research","academic"),("thesis","academic"),("cite","academic"),
                  ("content","creative"),("story","creative"),("blog","creative"),("design","creative")]:
        if kw in t: domain = d; break

    # S3: Expertise (1=beginner, 5=expert)
    expertise = 3
    beginner_markers = ["what is", "how do i", "i don't know", "explain", "simple", "basic", "beginner", "noob", "untuk pemula", "apa itu", "gimana caranya"]
    expert_markers   = ["implement", "optimize", "architecture", "idiomatic", "edge case", "tradeoff", "latency", "throughput", "refactor", "scalab"]
    if any(m in t for m in beginner_markers): expertise = 2
    if any(m in t for m in expert_markers):   expertise = 4
    # Very short terse query = probably expert
    if len(task.split()) <= 5 and "?" not in task: expertise = max(expertise, 4)

    # S4: Emotional signal
    emotion = "neutral"
    if any(m in t for m in ["asap","urgent","deadline","help!","tolong","buru","cepat"]): emotion = "urgent"
    elif any(m in t for m in ["still","again","doesn't work","still not","masih ga","kenapa ga"]): emotion = "frustrated"
    elif any(m in t for m in ["worried","scared","overwhelmed","anxious","takut","bingung banget","ga tau harus"]): emotion = "vulnerable"
    elif any(m in t for m in ["lol","btw","just wondering","haha","wkwk","iseng","santai"]): emotion = "casual"

    # S5: Stakes/scope
    stakes = "medium"
    if any(m in t for m in ["quick","short","brief","simple","just","cepet","singkat"]): stakes = "low"
    if any(m in t for m in ["production","deploy","contract","legal","medical","critical","penting banget","serius"]): stakes = "high"
    if any(m in t for m in ["life","death","emergency","darurat","nyawa","hukum","lawsuit"]): stakes = "critical"

    # Archetype mapping
    archetype_map = {
        "lookup": "A1", "explain": "A4", "generate": "A3", "fix": "A4",
        "analyze": "A4", "validate": "A8", "build": "A5", "decide": "A6",
        "orchestrate": "A7", "support": "A9",
    }

    return {
        "intent": intent,
        "domain": domain,
        "expertise": expertise,
        "emotion": emotion,
        "stakes": stakes,
        "archetype": archetype_map.get(intent, "A3"),
    }


def t_route(task: str, execute: bool = False):
    """
    Route a task through CORE Routing Engine v2.0.
    Extracts signals → determines archetype → builds system prompt → optionally executes via Groq.
    execute=True: actually runs the task and returns response.
    execute=False: returns routing analysis only (for inspection).
    """
    if not task: return {"ok": False, "error": "task required"}

    sig = extract_signals(task)

    # Complexity score
    complexity = 3  # base
    if sig["expertise"] <= 2:  complexity += 1
    if sig["emotion"] in ("urgent", "frustrated"): complexity += 1
    if sig["stakes"] == "critical": complexity += 2
    if sig["stakes"] == "high":     complexity += 1
    if sig["expertise"] >= 5:  complexity -= 1
    if sig["stakes"] == "low": complexity -= 1
    complexity = max(1, min(12, complexity))

    # Build system prompt calibrated to signals
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
        f"Domain context: {sig['domain']}. "
        f"Stakes level: {sig['stakes']}. "
        f"{disclaimer} "
        "Be genuinely helpful. If you see a gap the user didn't ask about but should know, mention it briefly."
    )

    routing_info = {
        "signals": sig,
        "complexity": complexity,
        "system_prompt_preview": system_prompt[:120] + "...",
        "archetype": sig["archetype"],
    }

    if not execute:
        return {"ok": True, "routing": routing_info}

    # Execute via Groq
    try:
        model = GROQ_FAST if complexity <= 4 else GROQ_MODEL
        response = groq_chat(system_prompt, task, model=model)
        # Log to task_queue as completed
        sb_post("task_queue", {
            "task": task[:300], "status": "completed", "priority": 5,
            "error": None,
            "chat_id": "",
        })
        return {"ok": True, "routing": routing_info, "response": response, "model_used": model}
    except Exception as e:
        return {"ok": False, "routing": routing_info, "error": str(e)}


def t_ask(question: str, domain: str = ""):
    """
    Ask CORE anything. Pulls relevant KB context + Groq generates answer.
    This is the main AGI query interface — smarter than raw search_kb.
    """
    if not question: return {"ok": False, "error": "question required"}

    # Pull KB context
    kb_results = t_search_kb(question, domain=domain, limit=5)
    kb_context = ""
    if kb_results:
        kb_context = "\n\n".join([
            f"[KB: {r.get('topic','')}]\n{str(r.get('content',''))[:300]}"
            for r in kb_results
        ])

    # Pull recent mistakes for the domain
    mistakes = t_get_mistakes(domain=domain or "general", limit=3)
    mistake_context = ""
    if mistakes:
        mistake_context = "\n".join([
            f"- Avoid: {m.get('what_failed','')} → {m.get('correct_approach','')[:100]}"
            for m in mistakes
        ])

    system = (
        "You are CORE, a personal AGI assistant with accumulated knowledge from many sessions. "
        "Answer using the knowledge base context provided. Be specific and actionable. "
        "If KB context is insufficient, say so and answer from general knowledge."
    )
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
    """
    Single-call hot reflection logger. Replaces manual sb_insert to hot_reflections.
    Use at end of any significant session or task.
    """
    ok = sb_post("hot_reflections", {
        "task_summary": task_summary[:300],
        "domain": domain,
        "verify_rate": 0.0,
        "mistake_consult_rate": 0.0,
        "new_patterns": patterns or [],
        "new_mistakes": [],
        "quality_score": quality,
        "gaps_identified": None,
        "reflection_text": notes or f"Logged via t_reflect. Domain: {domain}.",
        "processed_by_cold": False,
    })
    return {"ok": ok, "domain": domain, "patterns_count": len(patterns or [])}


def t_stats():
    """
    Analytics dashboard: domain breakdown, top patterns, routing distribution, mistake frequency.
    Derived from hot_reflections + pattern_frequency + mistakes tables.
    """
    try:
        # Domain breakdown from hot_reflections
        hots = sb_get("hot_reflections", "select=domain,quality_score&limit=200", svc=True)
        domain_counts: Counter = Counter(h.get("domain","general") for h in hots)

        # Top patterns
        patterns = sb_get("pattern_frequency",
                          "select=pattern_key,frequency,domain&order=frequency.desc&limit=10", svc=True)

        # Mistake frequency by domain
        mistakes = sb_get("mistakes", "select=domain&limit=200", svc=True)
        mistake_counts: Counter = Counter(m.get("domain","general") for m in mistakes)

        # Avg quality score
        scores = [h["quality_score"] for h in hots if h.get("quality_score") is not None]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else None

        # Counts
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
            "top_patterns": [{"pattern": p.get("pattern_key","")[:80],
                              "freq": p.get("frequency",0),
                              "domain": p.get("domain","")} for p in patterns],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_search_mistakes(query: str = "", domain: str = "", limit: int = 10):
    """
    Semantic mistake search. Returns what failed + correct approach.
    Better than get_mistakes for specific lookups.
    """
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
                               "desc": "Route a task through CORE Routing Engine v2.0. execute=true to run via Groq. Returns signals+archetype+response."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything. Pulls KB context + Groq generates answer. Main AGI query interface."},
    "reflect":                {"fn": t_reflect,                "perm": "WRITE",   "args": ["task_summary", "domain", "patterns", "quality", "notes"],
                               "desc": "Log a hot reflection in one call. Use at end of significant sessions."},
    "stats":                  {"fn": t_stats,                  "perm": "READ",    "args": [],
                               "desc": "Analytics: domain distribution, top patterns, mistake frequency, avg quality score."},
    "search_mistakes":        {"fn": t_search_mistakes,        "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Semantic mistake search. Returns what_failed + correct_approach. Better than get_mistakes for specific lookups."},
}

# ── MCP JSON-RPC ────────────────────────────────────────────────────────────────
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
                   "serverInfo": {"name": "CORE v5.0", "version": "5.0"}})
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

# ── FastAPI ────────────────────────────────────────────────────────────────────
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"

class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}

class PatchRequest(BaseModel):
    """Surgical patch request — used by claude.ai to avoid full-file rewrites."""
    secret: str
    path: str
    old_str: str
    new_str: str
    message: str
    repo: Optional[str] = ""

app = FastAPI(title="CORE v5.0", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    counts = get_system_counts()
    step = get_current_step()  # dynamic — never hardcode
    return {"service": "CORE v5.0", "step": step,
            "knowledge": counts.get("knowledge_base", 0), "sessions": counts.get("sessions", 0),
            "mistakes": counts.get("mistakes", 0),
            "hot_unprocessed": counts.get("hot_reflections", 0),
            "evolutions_pending": counts.get("evolution_queue", 0)}

@app.get("/health")
def health_ep(): return t_health()

@app.post("/patch")
async def patch_file(body: PatchRequest):
    """
    Surgical find-and-replace endpoint for claude.ai.
    Avoids full-file rewrites — send only the diff strings.
    Auth: body.secret must match MCP_SECRET.
    Fix 2026-03-11j: added this endpoint.
    """
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    result = t_gh_search_replace(
        path=body.path,
        old_str=body.old_str,
        new_str=body.new_str,
        message=body.message,
        repo=body.repo or GITHUB_REPO,
    )
    if result.get("ok"):
        notify(f"\u2702\ufe0f Patch applied: `{body.path}`\n{body.message[:100]}")
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
    step = get_current_step()  # dynamic — never hardcode
    notify(f"\U0001f50c MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {"session_token": tok, "expires_hours": SESSION_TTL_H,
            "state": t_state(), "health": t_health(), "constitution": t_constitution(),
            "tools": list(TOOLS.keys()), "note": f"CORE v5.0 ready. {step}"}

@app.post("/mcp/auth")
async def mcp_auth(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        notify(f"\u26a0\ufe0f Invalid MCP auth from {req.client.host}")
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
        notify(f"*CORE v5.1*\n{step}\n"
               f"Knowledge: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)}\n\n"
               f"*Commands:*\n"
               f"/status — health + training\n"
               f"/ask <question> — ask CORE anything (Groq)\n"
               f"/route <task> — see how CORE would route this\n"
               f"/stats — analytics dashboard\n"
               f"/mistakes [domain] — recent mistakes\n"
               f"/tasks — recent task queue\n"
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
        # Non-command message → route + execute via Groq
        sig = extract_signals(text)
        notify(f"⚙️ Routing [{sig['archetype']}] {sig['domain']}...", cid)
        result = t_route(text, execute=True)
        if result.get("ok") and result.get("response"):
            notify(result["response"][:1500], cid)
        else:
            # Fallback: queue it
            ok = sb_post("task_queue", {"task": text, "chat_id": cid, "status": "pending", "priority": 5})
            notify(f"✅ Queued: `{text[:80]}`" if ok else "❌ Failed to queue task.", cid)

def queue_poller():
    """Poll task_queue and EXECUTE pending tasks via routing engine v2.0."""
    print("[QUEUE] Started — v5.1 live execution mode")
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t = tasks[0]
                tid = t["id"]
                task_text = t.get("task", "")
                chat_id = t.get("chat_id", "")

                # Mark as processing
                sb_patch("task_queue", f"id=eq.{tid}", {"status": "processing"})
                print(f"[QUEUE] Executing task {tid}: {task_text[:60]}")

                # Route + execute
                result = t_route(task_text, execute=True)

                if result.get("ok") and result.get("response"):
                    sb_patch("task_queue", f"id=eq.{tid}", {"status": "completed", "error": None})
                    # Notify via Telegram if task came from Telegram
                    if chat_id:
                        notify(f"✅ *Task completed*\n{result['response'][:800]}", chat_id)
                    print(f"[QUEUE] Task {tid} completed")
                else:
                    err = result.get("error", "unknown error")
                    sb_patch("task_queue", f"id=eq.{tid}", {"status": "failed", "error": err[:200]})
                    if chat_id:
                        notify(f"❌ Task failed: {err[:200]}", chat_id)
                    print(f"[QUEUE] Task {tid} failed: {err}")

        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(10)

@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    # Fix 2026-03-11L: self_sync_check on every startup
    threading.Thread(target=self_sync_check, daemon=True).start()
    counts = get_system_counts()
    step = get_current_step()  # dynamic — never hardcode
    notify(f"*CORE v5.0 Online*\n{step}\nKnowledge: {counts.get('knowledge_base',0)}\n"
           f"Sessions: {counts.get('sessions',0)}\nMCP: {len(TOOLS)} tools active\n"
           f"Training: ✅ ACTIVE — hot/cold/evolution pipeline running")
    print(f"[CORE] v5.0 online :{PORT} — {step}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core:app", host="0.0.0.0", port=PORT, reload=False)
