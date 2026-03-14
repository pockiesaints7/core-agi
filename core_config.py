"""core_config.py — CORE AGI shared configuration
All env vars, constants, RateLimiter, and Supabase helpers.
Imported by all other core_* modules. Has NO imports from other core_* modules.

Part of Task 2 architecture split. core.py remains the live entry point until
smoke test passes on all modules.
"""
import json
import os
import time
from collections import defaultdict

import httpx

# -- Env vars ------------------------------------------------------------------
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
SUPABASE_PAT   = os.environ.get("SUPABASE_PAT", "")  # Management API PAT for DB introspection
SUPABASE_REF   = "qbfaplqiakwjvrtwpbmr"  # Project ref
PORT           = int(os.environ.get("PORT", 8080))
SESSION_TTL_H  = 8

MCP_PROTOCOL_VERSION = "2024-11-05"

# Training config
COLD_HOT_THRESHOLD        = 10
COLD_TIME_THRESHOLD       = 86400
COLD_KB_GROWTH_THRESHOLD  = 100
PATTERN_EVO_THRESHOLD     = 3
KNOWLEDGE_AUTO_CONFIDENCE = 0.7

# KB mining config
KB_MINE_BATCH_SIZE       = 20
KB_MINE_RATIO_THRESHOLD  = 20

# Self-sync config
CORE_SELF_STALE_DAYS = 7

# -- Rate limiter --------------------------------------------------------------
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

# -- Supabase helpers ----------------------------------------------------------
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

def sb_delete(t, m):
    """Delete rows matching filter m from table t. m must be a non-empty PostgREST filter string.
    Safety: refuses to run if m is empty -- never allows unfiltered full-table delete."""
    if not m or not str(m).strip():
        print(f"[SB DELETE] BLOCKED: empty filter on {t} -- full-table delete not allowed")
        return False
    if not L.sbw(): return False
    r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), timeout=15)
    if not r.is_success:
        print(f"[SB DELETE] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

def sb_delete(t, m):
    """DELETE rows matching filter string m from table t.
    m must be a non-empty PostgREST filter string e.g. 'id=eq.123'.
    Returns False immediately if m is empty -- never allows full-table delete."""
    if not m or not m.strip():
        print(f"[SB DELETE] BLOCKED: empty filter on table {t} -- full-table delete not allowed")
        return False
    if not L.sbw(): return False
    r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), timeout=15)
    if not r.is_success:
        print(f"[SB DELETE] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

# -- Groq chat helper ----------------------------------------------------------
def groq_chat(system: str, user: str, model: str = None, max_tokens: int = 1024) -> str:
    """Shared Groq chat helper. Matches core.py signature exactly."""
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
