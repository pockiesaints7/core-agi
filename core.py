"""CORE v5.0 — Step 3 | Training Pipeline Active
Owner: REINVAGNAR
Step 0: MCP fully working, Telegram queues tasks.
Step 1: Claude Desktop live connection.
Step 2: Training pipeline designed (TRAINING_DESIGN.md).
Step 3: Training pipeline implemented.
Fix (2026-03-11e): t_state() fetches operating_context.json + SESSION.md from GitHub.
Fix (2026-03-11f): cold_processor uses Counter for batch freq counting.
Fix (2026-03-11g): cold_reflections insert uses sb_post_critical (bypasses rate limiter).
  Root cause: sb_post() silently returns False when L.sbw() is exhausted. cold_reflections
  is a critical summary write (1x per batch run) so it must bypass the in-memory counter.
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
            "processed_by_cold": False,
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


def run_cold_processor():
    """Batch: distill patterns, upsert pattern_frequency, queue evolutions, write summary.
    Fix (2026-03-11g): cold_reflections uses sb_post_critical (bypasses rate limiter).
    """
    try:
        hots = sb_get("hot_reflections",
                      "select=id,domain,new_patterns,new_mistakes,quality_score&processed_by_cold=eq.false&id=gt.1&order=created_at.asc",
                      svc=True)
        if not hots:
            print("[COLD] No unprocessed hot reflections.")
            return {"ok": True, "processed": 0, "evolutions_queued": 0}

        period_start      = datetime.utcnow().isoformat()
        evolutions_queued = 0

        # Aggregate pattern counts across batch
        batch_counts: Counter = Counter()
        batch_domain: dict    = {}
        for h in hots:
            for p in (h.get("new_patterns") or []):
                if p:
                    key = str(p)[:200]
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))

        # Upsert pattern_frequency
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

        # Write cold_reflections summary — CRITICAL: use sb_post_critical, not sb_post
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

        # Mark all hots as processed
        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": True})

        if evolutions_queued > 0:
            notify(f"✨ Cold processor: {evolutions_queued} evolution(s) queued from {len(hots)} sessions.\nUse /evolutions to review.")

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
            notify(f"✅ Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
            print(f"[EVO] Applied #{evolution_id} ({change_type})")
        else:
            notify(f"❌ Evolution #{evolution_id} apply failed\nType: {change_type}")
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
        notify(f"❌ Evolution #{evolution_id} rejected.\nReason: {reason or 'No reason given'}")
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
            hots        = sb_get("hot_reflections", "select=id&processed_by_cold=eq.false&id=gt.1", svc=True)
            unprocessed = len(hots)
            time_since  = time.time() - _last_cold_run
            if unprocessed >= COLD_HOT_THRESHOLD or (time_since >= COLD_TIME_THRESHOLD and unprocessed > 0):
                print(f"[COLD] Triggering: unprocessed={unprocessed} time_since={int(time_since)}s")
                run_cold_processor()
                _last_cold_run = time.time()
            # Auto-apply high-confidence knowledge evolutions
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
        unprocessed = sb_get("hot_reflections", "select=id&processed_by_cold=eq.false&id=gt.1", svc=True)
        pending_evo = sb_get("evolution_queue", "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1", svc=True)
        return {"status": "Step 3 — training pipeline ACTIVE",
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
    "gh_search_replace":        {"fn": t_gh_search_replace, "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo"],
                                 "desc": "Surgical find-and-replace in a GitHub file. Fails safely if old_str not found or ambiguous."},
    "gh_read_lines":            {"fn": t_gh_read_lines,    "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                                 "desc": "Read specific line range from GitHub file with line numbers. Use before gh_search_replace."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write file to GitHub repo. repo defaults to pockiesaints7/core-agi"},
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

app = FastAPI(title="CORE v5.0", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    counts = get_system_counts()
    return {"service": "CORE v5.0", "step": "3 — Training Pipeline Active",
            "knowledge": counts.get("knowledge_base", 0), "sessions": counts.get("sessions", 0),
            "mistakes": counts.get("mistakes", 0),
            "hot_unprocessed": counts.get("hot_reflections", 0),
            "evolutions_pending": counts.get("evolution_queue", 0)}

@app.get("/health")
def health_ep(): return t_health()

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
    notify(f"\U0001f50c MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {"session_token": tok, "expires_hours": SESSION_TTL_H,
            "state": t_state(), "health": t_health(), "constitution": t_constitution(),
            "tools": list(TOOLS.keys()), "note": "CORE Step 3 ready. Training pipeline active."}

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
        notify(f"*CORE v5.0 — Step 3*\nKnowledge: {counts.get('knowledge_base',0)}\n"
               f"Sessions: {counts.get('sessions',0)}\n\nCommands: /status /tasks /ask /evolutions\n/approve <id> /reject <id>", cid)
    elif text == "/status":
        h = t_health(); counts = get_system_counts(); ts = t_training_status()
        notify(f"*Status*\nSupabase: {h['components'].get('supabase')}\nGroq: {h['components'].get('groq')}\n"
               f"Telegram: {h['components'].get('telegram')}\nGitHub: {h['components'].get('github')}\n\n"
               f"Knowledge: {counts.get('knowledge_base',0)}\nSessions: {counts.get('sessions',0)}\n"
               f"Hot unprocessed: {ts.get('unprocessed_hot',0)}\nPending evolutions: {ts.get('pending_evolutions',0)}", cid)
    elif text == "/tasks":
        tasks = sb_get("task_queue", "select=task,status&order=created_at.desc&limit=5")
        lines = "\n".join([f"- [{t.get('status')}] {t.get('task','')[:60]}" for t in tasks])
        notify(f"*Recent Tasks*\n\n{lines}" if tasks else "No tasks yet.", cid)
    elif text.startswith("/ask "):
        q = text[5:].strip()
        res = t_search_kb(q, limit=5)
        lines = "\n\n".join([f"*{x.get('topic','')}*\n{str(x.get('content',''))[:200]}" for x in res]) if res else "Nothing found."
        notify(lines, cid)
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
        ok = sb_post("task_queue", {"task": text, "chat_id": cid, "status": "pending", "priority": 5})
        notify(f"\u2705 Queued: `{text[:80]}`" if ok else "\u274c Failed to queue task.", cid)

def queue_poller():
    print("[QUEUE] Started")
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t = tasks[0]
                sb_patch("task_queue", f"id=eq.{t['id']}", {"status": "waiting", "error": "Execution engine not yet active (Step 5)"})
                print(f"[QUEUE] Task {t['id']} acknowledged")
        except Exception as e: print(f"[QUEUE] {e}")
        time.sleep(30)

@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    counts = get_system_counts()
    notify(f"*CORE v5.0 Online — Step 3*\nKnowledge: {counts.get('knowledge_base',0)}\n"
           f"Sessions: {counts.get('sessions',0)}\nMCP: 17 tools active\n"
           f"Training: ✅ ACTIVE — hot/cold/evolution pipeline running")
    print(f"[CORE] v5.0 Step 3 online :{PORT} — training pipeline active")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core:app", host="0.0.0.0", port=PORT, reload=False)
