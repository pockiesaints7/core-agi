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
from dotenv import load_dotenv
load_dotenv()  # loads ~/core-agi/.env automatically

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
PORT           = int(os.environ.get("PORT", 8081))
SESSION_TTL_H  = 8

MCP_PROTOCOL_VERSION = "2024-11-05"

# Training config
COLD_HOT_THRESHOLD        = 5   # lowered from 10 -- faster signal processing
COLD_TIME_THRESHOLD       = 21600  # 6h -- lowered from 24h for faster cold runs
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

    def tg(self):       return True  # no limit -- loop timer is the throttle
    def gh(self):       return self._ok("gh",  3600, self.c.get("github_pushes_per_hour", 20))
    def sbw(self):      return True  # no limit -- loop timer is the throttle
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
    """DELETE rows matching filter string m from table t.
    m must be a non-empty PostgREST filter string e.g. 'id=eq.123'.
    Returns False immediately if m is empty -- never allows full-table delete."""
    if not m or not str(m).strip():
        print(f"[SB DELETE] BLOCKED: empty filter on table {t} -- full-table delete not allowed")
        return False
    if not L.sbw(): return False
    r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), timeout=15)
    if not r.is_success:
        print(f"[SB DELETE] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

# -- Telegram notify helper ----------------------------------------------------
def notify(text: str, chat_id: str = "") -> bool:
    """Send a Telegram message. Falls back to TELEGRAM_CHAT if chat_id not given.
    Non-blocking on failure — always returns bool."""
    cid = chat_id or TELEGRAM_CHAT
    if not TELEGRAM_TOKEN or not cid:
        print(f"[NOTIFY] Cannot send — TOKEN or chat_id missing")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text[:4096], "parse_mode": "HTML"},
            timeout=10,
        )
        return r.is_success
    except Exception as e:
        print(f"[NOTIFY] Failed: {e}")
        return False


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


# -- Gemini chat helper with round-robin key rotation -------------------------
_GEMINI_KEYS = [k.strip() for k in os.getenv("GEMINI_KEYS", "").replace(" ", "").split(",") if k.strip()]
_GEMINI_KEY_INDEX = 0
_GEMINI_MODEL = "gemini-2.5-flash-lite"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

def gemini_chat(system: str, user: str, max_tokens: int = 2048, json_mode: bool = False) -> str:
    """LLM chat via OpenRouter (or Gemini direct as fallback).
    Drop-in replacement — all callers unchanged.
    json_mode=True: instructs model to return valid JSON only."""
    if OPENROUTER_API_KEY:
        prompt = f"{system}\n\n{user}" if system else user
        payload = {
            "model": OPENROUTER_MODEL,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_err = None
        for attempt in range(3):
            try:
                r = httpx.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://core-agi-production.up.railway.app",
                        "X-Title": "CORE AGI",
                    },
                    json=payload,
                    timeout=60,
                )
                if r.status_code == 429:
                    last_err = "429 rate limit"
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                last_err = str(e)
                continue
        raise RuntimeError(f"OpenRouter failed after 3 attempts. Last: {last_err}")

    # Fallback: Gemini direct (if OPENROUTER_API_KEY not set)
    global _GEMINI_KEY_INDEX
    if not _GEMINI_KEYS:
        raise RuntimeError("Neither OPENROUTER_API_KEY nor GEMINI_KEYS is set")
    attempts = len(_GEMINI_KEYS)
    last_err = None
    for _ in range(attempts):
        key = _GEMINI_KEYS[_GEMINI_KEY_INDEX % len(_GEMINI_KEYS)]
        _GEMINI_KEY_INDEX = (_GEMINI_KEY_INDEX + 1) % len(_GEMINI_KEYS)
        try:
            prompt = f"{system}\n\n{user}" if system else user
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent",
                params={"key": key},
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {
                          "maxOutputTokens": max_tokens,
                          "temperature": 0.1,
                          **({"responseMimeType": "application/json"} if json_mode else {})
                      },
                      "safetySettings": [
                          {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                          {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                          {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                          {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                      ]},
                timeout=30,
            )
            if r.status_code == 429:
                last_err = f"429 on key index {(_GEMINI_KEY_INDEX - 1) % len(_GEMINI_KEYS)}"
                time.sleep(2)
                continue
            r.raise_for_status()
            resp_json = r.json()
            candidate = resp_json.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                last_err = f"empty parts (finish={candidate.get('finishReason','?')})"
                continue
            return parts[0]["text"].strip()
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"All {attempts} Gemini keys exhausted. Last error: {last_err}")

def build_live_schema(supabase_ref: str = "", supabase_pat: str = "") -> dict:
    """
    Build schema registry from actual Supabase tables at startup.
    Merges live column definitions into _SB_SCHEMA at import time.
    Falls back gracefully (returns {}) if Management API unavailable or PAT missing.

    Args:
        supabase_ref: Supabase project ref. Defaults to module-level SUPABASE_REF.
        supabase_pat: Supabase Management API PAT. Defaults to module-level SUPABASE_PAT.
    """
    try:
        # Use passed args first, fall back to module-level constants
        ref = supabase_ref or SUPABASE_REF
        pat = supabase_pat or SUPABASE_PAT
        if not pat:
            print("[SCHEMA] build_live_schema: SUPABASE_PAT not set — skipping live fetch")
            return {}
        if not ref:
            print("[SCHEMA] build_live_schema: SUPABASE_REF not set — skipping live fetch")
            return {}

        resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
            },
            json={"query": """
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
            """},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(f"[SCHEMA] Live schema fetch failed: {resp.status_code} {resp.text[:200]}")
            return {}

        rows = resp.json()
        if not isinstance(rows, list):
            print(f"[SCHEMA] Unexpected response format: {type(rows)}")
            return {}

        live_schema: dict = {}
        for row in rows:
            table = row.get("table_name", "")
            col   = row.get("column_name", "")
            dtype = row.get("data_type", "text")
            if not table or not col:
                continue
            if table not in live_schema:
                live_schema[table] = {"columns": {}}
            live_schema[table]["columns"][col] = dtype

        print(f"[SCHEMA] Live schema loaded: {len(live_schema)} tables")
        return live_schema
    except Exception as e:
        print(f"[SCHEMA] Live schema failed (using hardcoded fallback): {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL CATEGORY KEYWORDS — single source of truth
# Used by: core_orchestrator._build_live_categories()
#          core_web.t_list_tools()
# Update here ONLY when adding a new tool category domain.
# Keys are category names. Values are keyword fragments matched against tool names.
# A tool is assigned to the first category whose keywords appear in the tool name.
# Tools that match no keywords go to "misc" automatically.
# ══════════════════════════════════════════════════════════════════════════════
TOOL_CATEGORY_KEYWORDS: dict = {
    "deploy":    ["redeploy", "deploy", "build_status", "validate_syntax",
                  "patch_file", "multi_patch", "gh_search_replace", "railway_logs",
                  "replace_fn", "smart_patch", "register_tool", "rollback"],
    "code":      ["read_file", "write_file", "gh_read", "search_in_file",
                  "core_py", "append_to_file", "diff"],
    "training":  ["cold_processor", "training_pipeline", "evolution", "reflection",
                  "backfill", "synthesize"],
    "system":    ["get_state", "health", "stats", "crash", "system_map",
                  "sync_system", "session_start", "session_end"],
    "railway":   ["railway_env", "railway_service", "railway_logs"],
    "knowledge": ["search_kb", "add_knowledge", "kb_update", "get_mistakes",
                  "search_mistakes", "ask"],
    "task":      ["task_add", "task_update", "task_health", "sb_query",
                  "sb_insert", "sb_patch", "sb_upsert", "sb_delete"],
    "crypto":    ["crypto", "binance"],
    "project":   ["project_"],
    "agentic":   ["reason_chain", "lookahead", "decompose", "negative_space",
                  "predict_failure", "action_gate", "loop_detect", "goal_check",
                  "circuit_breaker", "mid_task", "assert_source"],
    "web":       ["web_search", "web_fetch", "summarize_url"],
    "document":  ["create_document", "create_spreadsheet", "create_presentation",
                  "read_document", "convert_document", "read_pdf", "read_image"],
    "image":     ["generate_image", "image_process"],
    "utils":     ["weather", "calc", "datetime", "currency", "translate",
                  "run_python", "list_tools", "get_tool_info", "get_table_schema",
                  "notify", "tool_stats", "tool_health", "debug_fn", "backlog",
                  "changelog", "backup"],
}

# Tools always included in every model call regardless of category routing.
# These are the core brain tools — they should always be available.
TOOL_ALWAYS_INCLUDE: set = {
    "search_kb", "get_mistakes", "list_tools", "get_tool_info",
    "get_behavioral_rules", "get_table_schema",
}
