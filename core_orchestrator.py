"""
core_orchestrator.py — CORE Telegram Full-Power Agentic Orchestrator
=====================================================================
Token-optimised. Full-power. OpenRouter-primary.

PROVIDER CHAIN (all functions):
  1. OpenRouter  — primary for ALL calls (tool selection, history compression,
                   main reasoning, metacognition). Supports vision + long context.
  2. Gemini direct — fallback if OpenRouter fails/429
  3. Groq        — last resort, text only

REMOVED vs previous version:
  - _call_anthropic()  — not needed, OpenRouter handles Claude models too
  - _call_openai()     — not needed, OpenRouter handles GPT models too
  - All GROQ_FAST usage for cheap calls — replaced with OpenRouter fast model

METACOGNITIVE LAYER (new):
  - _reason_before_execute()  — think before first tool call
  - _validate_before_reply()  — self-check before sending answer to owner

MOUNT IN core_main.py (3 changes):
  # 1. Top-level import:
  from core_orchestrator import handle_telegram_message, start_orchestrator

  # 2. In on_start() after existing threading.Thread lines:
  start_orchestrator()

  # 3. In handle_msg(), replace the final else branch:
  else:
      threading.Thread(
          target=handle_telegram_message, args=(msg,), daemon=True
      ).start()

MODEL SWAP (one line):
  # Primary reasoning — pilih salah satu:
OPENROUTER_MODEL = "google/gemini-2.5-flash"        # best value
OPENROUTER_MODEL = "anthropic/claude-sonnet-4-5"    # best quality
OPENROUTER_MODEL = "google/gemini-2.5-pro"          # best context                   # if want GPT

VARIABLES VERIFIED AGAINST ACTUAL CODEBASE:
  core_config.py  : TELEGRAM_CHAT, SUPABASE_PAT, SUPABASE_REF,
                    sb_get(t, qs, svc), sb_post(t, d), sb_patch(t, m, d),
                    gemini_chat(system, user, max_tokens, json_mode),
                    groq_chat(system, user, model, max_tokens)
  core_tools.py   : TOOLS dict keys are fn/perm/args/desc
  core_main.py    : handle_msg(msg) uses cid/text, on_start() @app.on_event

PHASE 1 CHANGES (P1-03 + P1-06):
  P1-03: Trivial message fast-path — skips 6-fetch brain query for simple messages
  P1-06: Session-level READ tool result cache — deduplicates repeated read calls

PHASE 2 CHANGES (P2-03):
  P2-03: Cross-session goal injection — active_goals from session_goals table
         injected into system prompt as ACTIVE GOALS section
"""

import base64
import json
import os
import threading
import time
import traceback
import re
from collections import deque
from datetime import datetime
from typing import Optional

import httpx

from core_config import (
    SUPABASE_URL, SUPABASE_SVC, SUPABASE_PAT, SUPABASE_REF,
    TELEGRAM_TOKEN, TELEGRAM_CHAT,
    sb_get, sb_post, sb_patch,
    gemini_chat,
)
from core_github import notify  # notify(msg, cid=None)
# Schema helpers — lazily imported from core_tools to avoid circular import at module load
def _sel(table: str, extra_cols: list = None) -> str:
    """Safe SELECT string from live-merged schema. Excludes fat_columns.
    extra_cols that are fat_columns are silently dropped — use _sel_force if needed.
    Falls back to conservative hardcoded strings (never '*') on cold-start import error."""
    try:
        from core_tools import get_safe_select
        return get_safe_select(table, extra_cols)
    except Exception:
        _COLD_SAFE = {
            "knowledge_base":         "id,domain,topic,confidence,source,created_at",
            "task_queue":             "id,status,priority,source,next_step,blocked_by,created_at",
            "mistakes":               "id,domain,what_failed,severity,root_cause,created_at",
            "sessions":               "id,summary,domain,quality_score,created_at,resume_task",
            "hot_reflections":        "id,domain,quality_score,source,processed_by_cold,created_at",
            "cold_reflections":       "id,period_start,period_end,hot_count,patterns_found,evolutions_queued,created_at",
            "pattern_frequency":      "id,pattern_key,frequency,domain,auto_applied,last_seen",
            "telegram_conversations": "id,chat_id,role,created_at",
            "behavioral_rules":       "id,domain,trigger,confidence,active,source,created_at",
            "evolution_queue":        "id,change_type,status,confidence,source,created_at",
        }
        return _COLD_SAFE.get(table, "id,created_at")


def _sel_force(table: str, cols: list) -> str:
    """SELECT string that includes specific columns regardless of fat_column status.
    Use when you genuinely need a fat column (e.g. result, content, reflection_text).
    Validates against live schema if available — falls back to cols as-is."""
    try:
        from core_tools import get_table_cols
        known = get_table_cols(table)
        if known:
            valid = [c for c in cols if c in known]
            return ",".join(valid) if valid else ",".join(cols)
    except Exception:
        pass
    return ",".join(cols)


def _has_col(table: str, col: str) -> bool:
    """Return True if column exists in live schema for table."""
    try:
        from core_tools import get_table_cols
        return col in get_table_cols(table)
    except Exception:
        return True  # optimistic: don't block query if schema unavailable


# ══════════════════════════════════════════════════════════════════════════════
# MODEL CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Primary model for ALL calls — reasoning, tool selection, compression, metacognition
OPENROUTER_MODEL       = "google/gemini-2.5-flash"   # swap here to change model
OPENROUTER_FAST_MODEL  = "google/gemini-2.5-flash-lite"   # cheap calls: tool select, compress
                                                           # swap to "meta-llama/llama-3.3-70b-instruct:free"
                                                           # or any fast OR model if needed

MODEL_PROVIDER = "openrouter"  # display label only

# Gemini fallback model (direct API)
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"

# Groq last resort
GROQ_LAST_RESORT_MODEL = "meta-llama/llama-4-scout"

# Telegram document MIME types → extension hint
_TG_MIME_EXT = {
    "application/pdf":          "pdf",
    "application/msword":       "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":       "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain":               "txt",
    "text/csv":                 "csv",
    "image/jpeg":               "jpg",
    "image/png":                "png",
    "image/gif":                "gif",
    "image/webp":               "webp",
}

# ── Constants ──────────────────────────────────────────────────────────────────
MAX_HISTORY_TURNS     = 20
HISTORY_COMPRESS_AT   = 10
MAX_TOOL_CALLS        = 50
MAX_TOOL_RESULT_CHARS = 16000
MAX_CONTEXT_CHARS     = 16000
DESKTOP_TASK_TIMEOUT  = 300
SESSION_CACHE_TTL     = 1800
CONFIRM_TIMEOUT_SECS  = 120

# ── P1-06: READ tool result cache config ──────────────────────────────────────
_TOOL_CACHE_TTL = 300  # 5 minutes
_tool_result_cache: dict = {}   # key: (cid, tool_name, args_hash) → {result, ts}
_tool_cache_lock = threading.Lock()

# ── In-memory state ────────────────────────────────────────────────────────────
_conv_memory: dict      = {}
_conv_lock              = threading.Lock()
_pending_confirms: dict = {}
_confirm_lock           = threading.Lock()
_session_cache: dict    = {}
_cache_lock             = threading.Lock()
_active_loops: dict     = {}  # cid -> {"lock": Lock, "started_at": float, "message": str}
_active_lock            = threading.Lock()
LOOP_HARD_TIMEOUT       = 180  # seconds — force-release lock after this regardless
# ── Observability metrics (in-memory, reset on restart) ───────────────────────
_metrics: dict = {
    "total_messages":     0,
    "provider_or":        0,   # calls served by OpenRouter
    "provider_gemini":    0,   # calls served by Gemini fallback
    "provider_groq":      0,   # calls served by Groq last resort
    "provider_failed":    0,   # all providers failed
    "tool_calls_total":   0,
    "tool_calls_failed":  0,
    "loop_depths":        [],  # list of tool_call_count per completed loop
    "direct_answers":     0,   # loops short-circuited by pre-flight
    "trivial_fast_path":  0,   # P1-03: messages that skipped brain query
    "cache_hits":         0,   # P1-06: tool results served from cache
    "autonomous_runs":    0,   # P3-03: autonomous multi-step executions
    "implicit_positive":  0,   # P3-05: clean endings (no follow-up correction)
    "implicit_negative":  0,   # P3-05: correction signals detected
    "predictive_hits":    0,   # P3-06: context served from predictive cache
}
_metrics_lock = threading.Lock()

# ── P3-05: Implicit feedback tracking ─────────────────────────────────────────
# Tracks last reply time + content per cid for follow-up behavior detection
_last_reply: dict = {}          # cid → {ts, message, reply}
_last_reply_lock = threading.Lock()

# ── P3-06: Predictive context pre-load cache ──────────────────────────────────
_predictive_cache: dict = {}    # cid → {context_text, loaded_at, pattern_key}
_predictive_cache_lock = threading.Lock()
_PREDICTIVE_CACHE_TTL  = 1800   # 30 minutes
_PREDICTIVE_CHECK_INTERVAL = 1800  # run predictor every 30 min

# ── P3-03: Autonomous mode config ─────────────────────────────────────────────
_AUTONOMOUS_MAX_DEPTH  = 15     # max tool calls in autonomous sequence
_AUTONOMOUS_TRIGGERS   = [
    r'\bthen\b.*\bthen\b',          # "do X then Y then Z"
    r'\bafter\s+that\b',
    r'\bfollowed\s+by\b',
    r'\bkemudian\b',                # Indonesian: "then/after"
    r'\blalu\b',                    # Indonesian: "then"
    r'^\s*\d+\.\s',                 # numbered list at start of message
    r'step\s+\d',
    r'\bsequence\b',
    r'\bautomatically\b',
    r'\bin\s+one\s+go\b',
]
_AUTONOMOUS_TRIGGER_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _AUTONOMOUS_TRIGGERS]

def get_orchestrator_metrics() -> dict:
    """Return copy of current observability metrics."""
    with _metrics_lock:
        m = dict(_metrics)
        depths = m.get("loop_depths", [])
        m["avg_loop_depth"]  = round(sum(depths) / len(depths), 2) if depths else 0
        m["max_loop_depth"]  = max(depths) if depths else 0
        m["loop_depth_p90"]  = sorted(depths)[int(len(depths) * 0.9)] if len(depths) >= 10 else None
        m.pop("loop_depths")
        return m


# ══════════════════════════════════════════════════════════════════════════════
# P1-03: TRIVIAL MESSAGE CLASSIFIER
# Skips 6-fetch brain query for messages that don't need it.
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate a trivial/factual message needing no brain pre-fetch
_TRIVIAL_PATTERNS = [
    r'\btime\b', r'\bjam\b', r'\bwaktu\b',              # time queries
    r'\bweather\b', r'\bcuaca\b',                         # weather
    r'\bcalc\b', r'^\s*[\d\s\+\-\*\/\(\)\.]+\s*$',      # calculator / pure math
    r'\btranslate\b', r'\bterjemah',                      # translation
    r'^\s*(hi|hello|hey|halo|hai|selamat|good\s)',        # greetings
    r'\bprice\b', r'\bharga\b',                           # price checks
    r'\bping\b', r'\bstatus\b',                           # quick status
    r'\bcurrency\b', r'\bkurs\b',                         # currency
]
_TRIVIAL_RE = [re.compile(p, re.IGNORECASE) for p in _TRIVIAL_PATTERNS]

# Short messages (under this length) with no special keywords are also trivial
_TRIVIAL_MAX_LEN = 40


def _is_trivial(message: str) -> bool:
    """Return True if this message is simple enough to skip brain pre-fetch.

    Trivial = matches a known-fast pattern OR is very short AND contains no
    keywords that suggest KB/task/deployment work is needed.
    """
    msg = message.strip()

    # Never trivial if message mentions these high-value keywords
    _NON_TRIVIAL_KW = [
        "task", "deploy", "patch", "evolution", "error", "broken", "fix",
        "kb", "knowledge", "mistake", "pattern", "session", "analyze",
        "investigate", "why", "how", "train", "cold", "hot", "build",
        "railway", "supabase", "github", "tool", "function", "code",
        "tugas", "salah", "kenapa", "gimana", "coba", "tolong",
    ]
    msg_lower = msg.lower()
    if any(kw in msg_lower for kw in _NON_TRIVIAL_KW):
        return False

    # Match against trivial patterns
    for pattern in _TRIVIAL_RE:
        if pattern.search(msg):
            return True

    # Very short message with no non-trivial keywords = trivial
    if len(msg) <= _TRIVIAL_MAX_LEN:
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# P1-06: SESSION-LEVEL READ CACHE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(cid: str, tool_name: str, tool_args: dict) -> str:
    args_hash = str(hash(json.dumps(tool_args, sort_keys=True, default=str)))
    return f"{cid}::{tool_name}::{args_hash}"


def _cache_get(cid: str, tool_name: str, tool_args: dict) -> Optional[str]:
    """Return cached result if still valid, else None."""
    key = _cache_key(cid, tool_name, tool_args)
    with _tool_cache_lock:
        entry = _tool_result_cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > _TOOL_CACHE_TTL:
            del _tool_result_cache[key]
            return None
        return entry["result"]


def _cache_set(cid: str, tool_name: str, tool_args: dict, result: str):
    """Store result in cache."""
    key = _cache_key(cid, tool_name, tool_args)
    with _tool_cache_lock:
        _tool_result_cache[key] = {"result": result, "ts": time.time()}
        # Evict old entries if cache grows too large (max 500 entries)
        if len(_tool_result_cache) > 500:
            cutoff = time.time() - _TOOL_CACHE_TTL
            stale = [k for k, v in _tool_result_cache.items() if v["ts"] < cutoff]
            for k in stale:
                del _tool_result_cache[k]


def _cache_clear(cid: str):
    """Clear all cache entries for a cid (called on /clear)."""
    with _tool_cache_lock:
        keys = [k for k in _tool_result_cache if k.startswith(f"{cid}::")]
        for k in keys:
            del _tool_result_cache[k]


def _is_cacheable_tool(tool_name: str) -> bool:
    """Return True if this tool's results are safe to cache.
    Only READ-perm tools. Excludes tools whose results change every call.
    """
    try:
        from core_tools import TOOLS
        tdef = TOOLS.get(tool_name, {})
        perm = tdef.get("perm", "READ")
        if perm != "READ":
            return False
    except Exception:
        return False

    # Explicitly exclude always-dynamic READ tools
    _NEVER_CACHE = {
        "get_state", "get_system_health", "stats", "build_status",
        "deploy_status", "crash_report", "get_training_pipeline",
        "get_quality_alert", "task_health", "datetime_now",
        "weather", "crypto_price", "currency",
    }
    return tool_name not in _NEVER_CACHE


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _strip_json(s: str) -> str:
    """Strip markdown code fences from model JSON output."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _or_call(payload: dict, timeout: int = 60) -> dict:
    """
    Raw OpenRouter /v1/chat/completions call.
    Returns parsed response dict. Raises on non-2xx or missing key.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://core-agi-production.up.railway.app",
            "X-Title":       "CORE AGI",
        },
        json=payload,
        timeout=timeout,
    )
    if r.status_code == 429:
        # Retry once with backoff before raising
        time.sleep(5)
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://core-agi-production.up.railway.app",
                "X-Title":       "CORE AGI",
            },
            json=payload,
            timeout=timeout,
        )
        if r.status_code == 429:
            raise RuntimeError("OpenRouter 429 rate limited after retry")
    r.raise_for_status()
    return r.json()


def _or_text(system: str, user: str, model: str = None,
             max_tokens: int = 512, json_mode: bool = False) -> str:
    """
    Simple OpenRouter text call — returns raw string.
    Used for cheap operations: tool selection, history compression, metacognition.
    Fallback: Gemini direct → Groq.
    """
    m = model or OPENROUTER_FAST_MODEL
    payload = {
        "model":       m,
        "max_tokens":  max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    # 1. OpenRouter
    try:
        data = _or_call(payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned empty choices")
        with _metrics_lock: _metrics["provider_or"] += 1
        return choices[0]["message"].get("content") or ""
    except Exception as e:
        print(f"[ORCH] _or_text OpenRouter failed: {e}")

    # 2. Gemini direct
    try:
        result = gemini_chat(
            system=system, user=user,
            max_tokens=max_tokens, json_mode=json_mode,
        )
        with _metrics_lock: _metrics["provider_gemini"] += 1
        return result
    except Exception as e:
        print(f"[ORCH] _or_text Gemini failed: {e}")

    # 3. Groq last resort
    try:
        from core_config import groq_chat
        result = groq_chat(
            system=system, user=user,
            model=GROQ_LAST_RESORT_MODEL, max_tokens=max_tokens,
        )
        with _metrics_lock: _metrics["provider_groq"] += 1
        return result
    except Exception as e:
        with _metrics_lock: _metrics["provider_failed"] += 1
        raise RuntimeError(f"All providers failed in _or_text: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN-OPTIMISED TOOL SELECTION — via OpenRouter
# ══════════════════════════════════════════════════════════════════════════════

# ── Tools always provided to model on every message ──────────────────────────
_ALWAYS_TOOLS = {
    "search_kb", "get_mistakes", "list_tools", "get_tool_info",
    "get_behavioral_rules", "get_table_schema",
}

# ── Category keyword map — ONLY thing you ever update when adding a new domain ─
_CATEGORY_KEYWORDS = {
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

# Module-level cache — rebuilt whenever TOOLS size changes
_cat_cache: dict = {}
_cat_cache_size: int = 0


def _build_live_categories() -> dict:
    """Build tool→category mapping live from TOOLS dict using _CATEGORY_KEYWORDS."""
    global _cat_cache, _cat_cache_size
    try:
        from core_tools import TOOLS
        current_size = len(TOOLS)
        if _cat_cache and current_size == _cat_cache_size:
            return _cat_cache

        cats: dict = {cat: [] for cat in _CATEGORY_KEYWORDS}
        cats["misc"] = []

        for tool_name in TOOLS.keys():
            assigned = False
            for cat, keywords in _CATEGORY_KEYWORDS.items():
                if any(kw in tool_name for kw in keywords):
                    cats[cat].append(tool_name)
                    assigned = True
                    break
            if not assigned:
                cats["misc"].append(tool_name)

        cats = {k: v for k, v in cats.items() if v}

        _cat_cache = cats
        _cat_cache_size = current_size
        misc_count = len(cats.get("misc", []))
        print(f"[ORCH] Categories built live: {len(cats)} cats, "
              f"{current_size} tools total, {misc_count} in misc")
        return cats
    except Exception as e:
        print(f"[ORCH] _build_live_categories error: {e}")
        return {"misc": []}


def _select_tools(message: str, history_summary: str) -> list:
    """Select relevant tools for this message via LLM category routing."""
    try:
        from core_tools import TOOLS
        all_tool_names = set(TOOLS.keys())
        tool_cats = _build_live_categories()
        categories_text = ", ".join(tool_cats.keys())
        raw = _or_text(
            system=(
                "You are a tool router. Given a user message, output ONLY a JSON array "
                f"of category names from: [{categories_text}]. "
                "Rules:\n"
                "- Always include 'utils' if message asks about tools, capabilities, or what you can do\n"
                "- Always include 'knowledge' if message asks about KB, memory, or what you know\n"
                "- Always include 'system' if message asks about health, status, or counts\n"
                "- Always include 'task' if message mentions tasks, queue, or pending work\n"
                "- Include 'misc' if the request seems unusual or uncategorised\n"
                "Output only valid JSON array of strings, no preamble."
            ),
            user=f"Message: {message[:300]}\nHistory: {history_summary[:200]}",
            max_tokens=80,
            json_mode=True,
        )
        selected_cats = json.loads(_strip_json(raw))
        if not isinstance(selected_cats, list):
            raise ValueError("not a list")
        selected_tools = set(_ALWAYS_TOOLS)
        for cat in selected_cats:
            selected_tools.update(tool_cats.get(cat, []))
        result = [t for t in selected_tools if t in all_tool_names]
        print(f"[ORCH] Tool selection: {len(result)} tools for categories {selected_cats}")
        return result
    except Exception as e:
        print(f"[ORCH] tool selection fallback (all tools): {e}")
        try:
            from core_tools import TOOLS
            return list(TOOLS.keys())
        except Exception:
            return list(_ALWAYS_TOOLS)


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CONTEXT — cached, loaded once per SESSION_CACHE_TTL
# ══════════════════════════════════════════════════════════════════════════════


def _build_dynamic_context(recent_text: str = "") -> str:
    """
    P2-05: Message-scoped semantic context loading.
    Queries Supabase for KB, mistakes, patterns and reflections relevant to
    the current message. All three KB fetches use per-message keyword extraction
    so context is targeted, not generic session-level data.
    Returns a formatted string injected into the system prompt as PRECEDENTS.
    """
    parts = []
    recent_lower = recent_text.lower()[:200]

    import re as _re
    stop = {"the","a","an","is","are","was","were","i","you","we","it","to","of",
            "and","or","in","on","at","for","with","do","did","can","could","would",
            "please","kalau","yang","ada","ini","itu","ke","dari","sudah","bisa",
            "mau","perlu","tidak","bukan","tapi","juga","saya","kamu","dia",
            "that","this","have","from","they","them","will","what","when","where",
            "brp","gitu","udah","mana","kami","core","tool","tools"}
    words = [w for w in _re.findall(r"[a-zA-Z]{5,}", recent_lower) if w not in stop]
    keywords = list(dict.fromkeys(words))[:4]

    results = {}

    def _fetch_kb():
        try:
            if not keywords:
                return
            kw_filters = ",".join(f"topic.ilike.*{k}*" for k in keywords[:2])
            rows = sb_get(
                "knowledge_base",
                f"select={_sel_force('knowledge_base', ['domain','topic','content','source_type'])}"
                f"&or=({kw_filters})"
                f"&active=eq.true&order=confidence.desc&limit=5",
                svc=True,
            ) or []
            if not rows and keywords:
                rows = sb_get(
                    "knowledge_base",
                    f"select={_sel_force('knowledge_base', ['domain','topic','content','source_type'])}"
                    f"&topic=ilike.*{keywords[0]}*"
                    f"&active=eq.true&order=confidence.desc&limit=5",
                    svc=True,
                ) or []
            if rows:
                lines = []
                for r in rows:
                    topic   = r.get("topic", "")
                    content = r.get("content", "")[:300]
                    domain  = r.get("domain", "")
                    stype   = r.get("source_type", "")
                    tag = f"[{domain}]" + (f"[{stype}]" if stype else "")
                    lines.append(f"  {tag} {topic}: {content}")
                results["kb"] = "RELEVANT KB ENTRIES:\n" + "\n".join(lines)
        except Exception as e:
            print(f"[ORCH] dynamic KB fetch failed: {e}")

    def _fetch_mistakes():
        """P2-05: Message-scoped mistake fetch using per-message keywords."""
        try:
            if not keywords:
                # No keywords — fall back to generic recent mistakes
                rows = sb_get(
                    "mistakes",
                    f"select={_sel_force('mistakes', ['domain','what_failed','correct_approach','how_to_avoid'])}"
                    f"&id=gt.1&order=created_at.desc&limit=4",
                    svc=True,
                ) or []
            else:
                # Scoped: search what_failed + context + how_to_avoid for message keywords
                kw = keywords[0]
                rows = sb_get(
                    "mistakes",
                    f"select={_sel_force('mistakes', ['domain','what_failed','correct_approach','how_to_avoid'])}"
                    f"&id=gt.1"
                    f"&or=(what_failed.ilike.*{kw}*,context.ilike.*{kw}*,how_to_avoid.ilike.*{kw}*)"
                    f"&order=created_at.desc&limit=4",
                    svc=True,
                ) or []
                # Fallback to recent if scoped search returned nothing
                if not rows:
                    rows = sb_get(
                        "mistakes",
                        f"select={_sel_force('mistakes', ['domain','what_failed','correct_approach','how_to_avoid'])}"
                        f"&id=gt.1&order=created_at.desc&limit=3",
                        svc=True,
                    ) or []
            if rows:
                lines = []
                for r in rows:
                    wf  = r.get("what_failed", "")[:100]
                    fix = r.get("correct_approach") or r.get("how_to_avoid") or ""
                    dom = r.get("domain", "?")
                    lines.append(f"  [{dom}] AVOID: {wf} → {fix[:100]}")
                results["mistakes"] = "RELEVANT MISTAKES:\n" + "\n".join(lines)
        except Exception as e:
            print(f"[ORCH] dynamic mistakes fetch failed: {e}")

    def _fetch_patterns():
        """P2-05: Keyword-scoped pattern fetch when keywords exist, else top by frequency."""
        try:
            if keywords:
                kw = keywords[0]
                rows = sb_get(
                    "pattern_frequency",
                    f"select={_sel_force('pattern_frequency', ['pattern_key','frequency','domain','description'])}"
                    f"&id=gt.1&stale=eq.false"
                    f"&or=(pattern_key.ilike.*{kw}*,description.ilike.*{kw}*)"
                    f"&order=frequency.desc&limit=4",
                    svc=True,
                ) or []
                if not rows:
                    # No keyword match — fall back to top global patterns
                    rows = sb_get(
                        "pattern_frequency",
                        f"select={_sel_force('pattern_frequency', ['pattern_key','frequency','domain','description'])}"
                        f"&id=gt.1&stale=eq.false&order=frequency.desc&limit=4",
                        svc=True,
                    ) or []
            else:
                rows = sb_get(
                    "pattern_frequency",
                    f"select={_sel_force('pattern_frequency', ['pattern_key','frequency','domain','description'])}"
                    f"&id=gt.1&stale=eq.false&order=frequency.desc&limit=6",
                    svc=True,
                ) or []
            if rows:
                lines = []
                for r in rows:
                    pk   = r.get("pattern_key", "")
                    freq = r.get("frequency", 0)
                    desc = r.get("description", "")[:200]
                    dom  = r.get("domain", "")
                    lines.append(f"  [{dom}/{freq}x] {pk}: {desc}")
                results["patterns"] = "TOP RECURRING PATTERNS:\n" + "\n".join(lines)
        except Exception as e:
            print(f"[ORCH] dynamic patterns fetch failed: {e}")

    def _fetch_reflections():
        try:
            rows = sb_get(
                "cold_reflections",
                f"select={_sel_force('cold_reflections', ['summary_text','created_at','patterns_found'])}"
                f"&id=gt.1&order=id.desc&limit=3",
                svc=True,
            ) or []
            if rows:
                lines = []
                for r in rows:
                    date    = str(r.get("created_at", ""))[:10]
                    summary = r.get("summary_text", "")[:300]
                    pcount  = r.get("patterns_found", 0)
                    lines.append(f"  [{date}/{pcount} patterns] {summary}")
                results["reflections"] = "RECENT DISTILLED LEARNINGS:\n" + "\n".join(lines)
        except Exception as e:
            print(f"[ORCH] dynamic reflections fetch failed: {e}")

    import threading as _threading
    threads = [
        _threading.Thread(target=_fetch_kb,          daemon=True),
        _threading.Thread(target=_fetch_mistakes,    daemon=True),  # P2-05: new
        _threading.Thread(target=_fetch_patterns,    daemon=True),
        _threading.Thread(target=_fetch_reflections, daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=4)

    if not results:
        return ""

    sections = []
    if results.get("kb"):          sections.append(results["kb"])
    if results.get("mistakes"):    sections.append(results["mistakes"])   # P2-05
    if results.get("patterns"):    sections.append(results["patterns"])
    if results.get("reflections"): sections.append(results["reflections"])

    if not sections:
        return ""

    return "DYNAMIC CONTEXT (live from brain):\n" + "\n\n".join(sections) + "\n"

def _build_system_prompt(cid: str, recent_text: str = "") -> str:
    with _cache_lock:
        cached = _session_cache.get(cid)
        if cached and (time.time() - cached["loaded_at"]) < SESSION_CACHE_TTL:
            return cached["system_prompt"]

    parts = [
        "You are CORE — a sovereign intelligence, not an assistant.\n"
        "Owner: REINVAGNAR (Jakarta, WIB/UTC+7). Electrical Engineer, Manager at LSEI. "
        "Operates primarily at night. Expects execution, not questions.\n\n"

        "CORE OPERATING MANDATE:\n"
        "— You own problems end-to-end. If asked to do X, you figure out HOW and DO IT.\n"
        "— You are creative. No dedicated tool? Build the solution with run_python.\n"
        "— run_python is your superpower: it can call any HTTP API, parse any data, "
        "automate anything, interact with any service. Use it freely when no specific tool exists.\n"
        "— Never say 'I cannot do this' before attempting with run_python or tool combinations.\n"
        "— Speed matters. Don't ask permission for low-risk actions — execute and report.\n"
        "— Verify from Supabase/PC before acting on memory alone. But verify fast, not bureaucratically.\n"
        "— Think laterally: what COMBINATION of tools solves this? What can run_python do here?"
    ]

    try:
        from core_tools import t_session_start
        ss = t_session_start()
        if ss.get("ok"):
            counts     = ss.get("counts", {})
            in_prog    = ss.get("in_progress_tasks", []) or []
            mistakes   = ss.get("domain_mistakes", []) or []
            patterns   = ss.get("top_patterns", []) or []
            qa           = ss.get("quality_alert")
            live_tools   = ss.get("live_tool_count", 0)
            active_goals = ss.get("active_goals", []) or []

            parts.append(
                f"STATE: KB={counts.get('knowledge_base',0)} "
                f"Sessions={counts.get('sessions',0)} "
                f"Mistakes={counts.get('mistakes',0)} "
                f"Tools={live_tools}"
            )
            if in_prog:
                raw   = in_prog[0].get("task", "")
                title = raw[:120] if isinstance(raw, str) else str(raw)[:120]
                parts.append(f"RESUME TASK: {title}")
            if mistakes:
                m_lines = " | ".join(
                    f"[{m.get('domain','?')}] {m.get('what_failed','')[:80]}"
                    for m in mistakes[:3]
                )
                parts.append(f"AVOID: {m_lines}")
            if patterns:
                p_lines = " | ".join(
                    (p.get("pattern_key") or p.get("pattern",""))[:100]
                    + (f" ({p.get('frequency','')}x)" if p.get("frequency") else "")
                    for p in patterns[:5]
                )
                parts.append(f"TOP PATTERNS: {p_lines}")
            if qa:
                parts.append(f"QUALITY ALERT: {qa}")
            if active_goals:
                goal_lines = "\n".join(
                    f"  [{g.get('domain','')}] {g.get('goal','')} | progress: {(g.get('progress') or 'not started')[:100]}"
                    for g in active_goals[:5]
                )
                parts.append(f"ACTIVE GOALS (cross-session — P2-03):\n{goal_lines}")

            # P3-02: Owner profile injection
            owner_profile = ss.get("owner_profile", []) or []
            if owner_profile:
                by_dim: dict = {}
                for entry in owner_profile[:10]:
                    dim = entry.get("dimension", "?")
                    val = (entry.get("value") or "")[:120]
                    conf = float(entry.get("confidence") or 0)
                    if dim not in by_dim:
                        by_dim[dim] = []
                    by_dim[dim].append(f"{val} (conf={conf:.2f})")
                dim_lines = "\n".join(
                    f"  [{dim}] " + " | ".join(vals[:2])
                    for dim, vals in by_dim.items()
                )
                parts.append(f"OWNER PROFILE (P3-02):\n{dim_lines}")

            # P3-07: Weak capability domains warning
            weak_cap = ss.get("weak_capability_domains", []) or []
            if weak_cap:
                parts.append(
                    "CAPABILITY WARNING (P3-07): Domains below 0.60 reliability: "
                    + ", ".join(weak_cap)
                    + ". Flag uncertainty when operating in these domains."
                )

            dynamic_ctx = _build_dynamic_context(recent_text)
            if dynamic_ctx:
                parts.append(dynamic_ctx)

            # P3-04: Retrieve semantically relevant past conversation episodes
            if recent_text and cid:
                try:
                    from core_embeddings import retrieve_relevant_episodes
                    episodes = retrieve_relevant_episodes(cid, recent_text, limit=3)
                    if episodes:
                        ep_lines = "\n".join(
                            f"  [{e.get('turn_start','?')[:10]}] {e.get('summary','')[:200]}"
                            for e in episodes
                        )
                        parts.append(f"RELEVANT PAST CONVERSATIONS (P3-04):\n{ep_lines}")
                except Exception as _ep_e:
                    print(f"[P3-04] episode retrieval error (non-fatal): {_ep_e}")

            rules = ss.get("behavioral_rules", []) or []
            if rules:
                r_lines = "\n".join(
                    f"  [{r.get('trigger','')}] {r.get('pointer','')[:100]}"
                    for r in rules[:40]  # P1-07: show up to 40 rules
                )
                parts.append(f"BEHAVIORAL RULES:\n{r_lines}")
    except Exception as e:
        print(f"[ORCH] session_start error (non-fatal): {e}")
        try:
            dynamic_ctx = _build_dynamic_context(recent_text)
            if dynamic_ctx:
                parts.append(dynamic_ctx)
        except Exception:
            pass

    parts.append(
        "EXECUTION PHILOSOPHY:\n"
        "Fast path: simple query/lookup → answer directly, skip heavy reasoning gate.\n"
        "Standard path: execution task → ground → plan → execute → report.\n"
        "Creative path: no obvious tool → think laterally → run_python + API calls.\n"
        "Escalate only: truly destructive/irreversible → confirm with owner first.\n\n"

        "CREATIVE TOOLKIT — use these paths before saying 'cannot':\n"
        "• run_python → call ANY HTTP API (Telegram Bot API, GitHub, Supabase, anything)\n"
        "• run_python → parse files, process data, generate reports, automate tasks\n"
        "• run_python + web_fetch → scrape + process any web content\n"
        "• run_python + sb_query → analytics on any Supabase table\n"
        "• desktop_run_script → PowerShell/Python on owner's PC for local operations\n"
        "• Chaining: read_file → run_python(process) → write_file → notify owner\n"
        "• Telegram Bot API: run_python calling api.telegram.org — setMyCommands, sendMessage, etc\n"
        "• No dedicated tool exists? run_python IS the tool. Always try this first.\n\n"

        "BRAIN — always query before acting:\n"
        "search_kb, get_mistakes, get_behavioral_rules — always allowed, even in plan-only mode.\n"
        "search_kb empty → sb_query the table directly. Never declare UNKNOWN after 1 failed attempt.\n\n"

        "PRIME DIRECTIVE — GRAM:\n"
        "Every output: Grounded, Reasoned, Accurate, Minimal, Honest.\n"
        "Grounded = verified from tools/Supabase, not assumed from memory.\n"
        "Reasoned = explain the why, not just the what.\n"
        "Accurate = confident claims only when confirmed this session.\n"
        "Minimal = no fluff, no over-explanation, direct delivery.\n"
        "Honest = own errors immediately. Never simulate success.\n\n"

        "CORE PRINCIPLES (condensed):\n"
        "1. GROUND: verify from persistent brain before acting. Source hierarchy: tool result > Supabase > owner input > inference.\n"
        "2. UNDERSTAND: intent over literal words. Real goal? Real context? Think before executing.\n"
        "3. CREATIVE EXECUTION: no tool = build with run_python. Think combinations, not single tools. Never give up after one path fails.\n"
        "4. HONEST: CONFIRMED = verified this session. INFERRED = reasoned. UNKNOWN = exhausted alternatives. Label when precision matters.\n"
        "5. CLOSE THE LOOP: every action has outcome — report it explicitly. Every error → capture to brain. Every session → brain must improve.\n"
        "6. PROACTIVE: surface issues before owner notices — but only signal/noise ratio above threshold.\n"
        "7. SOVEREIGN GROWTH: improve every session. Challenge assumptions. Store every new pattern.\n"
        "8. OWNER COVENANT: credentials sacred. Never expose. Act in owner interest always.\n"
        "Domain note: rarl/* = simulation artifacts. core_agi/* = actual CORE execution history.\n"
    )

    full = "\n\n".join(parts)
    if len(full) > MAX_CONTEXT_CHARS:
        while len("\n\n".join(parts)) > MAX_CONTEXT_CHARS and len(parts) > 2:
            parts.pop()
        full = "\n\n".join(parts)
    prompt = full
    with _cache_lock:
        _session_cache[cid] = {"system_prompt": prompt, "loaded_at": time.time()}
    return prompt


def _invalidate_cache(cid: str = None):
    with _cache_lock:
        if cid:
            _session_cache.pop(cid, None)
        else:
            _session_cache.clear()


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION HISTORY — rolling summary compression via OpenRouter
# ══════════════════════════════════════════════════════════════════════════════

def _sb_save_msg(cid: str, role: str, content: str):
    try:
        sb_post("telegram_conversations", {
            "chat_id":    cid,
            "role":       role,
            "content":    content[:1500],
            "created_at": datetime.utcnow().isoformat(),
        })
    except Exception:
        pass


def _sb_load_history(cid: str) -> list:
    try:
        rows = sb_get(
            "telegram_conversations",
            f"select={_sel_force('telegram_conversations', ['id','role','content','created_at'])}"
            f"&chat_id=eq.{cid}"
            f"&deleted=eq.false"
            f"&order=created_at.desc"
            f"&limit={MAX_HISTORY_TURNS}",
            svc=True,
        ) or []
        return list(reversed(rows))
    except Exception:
        return []


def _get_history(cid: str) -> list:
    with _conv_lock:
        if cid not in _conv_memory:
            rows = _sb_load_history(cid)
            _conv_memory[cid] = deque(
                [{"role": r["role"], "content": r["content"],
                  "ts": r.get("created_at", "")} for r in rows],
                maxlen=MAX_HISTORY_TURNS,
            )
        return list(_conv_memory[cid])


def _append_history(cid: str, role: str, content: str,
                    image_b64: str = None, image_mime: str = None):
    entry = {"role": role, "content": content[:1500], "ts": datetime.utcnow().isoformat()}
    if image_b64:
        entry["image_b64"]  = image_b64
        entry["image_mime"] = image_mime or "image/jpeg"
    with _conv_lock:
        if cid not in _conv_memory:
            _conv_memory[cid] = deque(maxlen=MAX_HISTORY_TURNS)
        q = _conv_memory[cid]
        if len(q) >= MAX_HISTORY_TURNS - 2:
            _compress_history(q, cid=cid)  # P3-04: pass cid for episode storage
        q.append(entry)
    _sb_save_msg(cid, role, content)


def _compress_history(q: deque, cid: str = ""):
    """P3-04: Compress oldest N turns into summary + persist as conversation episode.
    Episode is embedded and stored in conversation_episodes table for future semantic retrieval.
    Falls back to simple in-memory summary if episode storage fails.
    """
    if len(q) < HISTORY_COMPRESS_AT:
        return
    oldest = [q.popleft() for _ in range(HISTORY_COMPRESS_AT) if q]
    try:
        text = "\n".join(
            f"{e['role'].upper()}: {e['content'][:200]}" for e in oldest
        )
        summary = _or_text(
            system="Summarise this conversation segment in 2-3 sentences. Be factual, include outcomes.",
            user=text,
            max_tokens=150,
        )
        # P3-04: persist as episode if cid provided
        if cid and summary:
            try:
                from core_embeddings import compress_to_episode
                ep = compress_to_episode(cid, oldest)
                if ep.get("ok"):
                    print(f"[P3-04] Episode stored id={ep.get('episode_id')} tags={ep.get('tags')}")
            except Exception as _ep_e:
                print(f"[P3-04] episode storage error (non-fatal): {_ep_e}")

        q.appendleft({
            "role":    "system",
            "content": f"[HISTORY SUMMARY] {summary}",
            "ts":      oldest[0].get("ts", ""),
        })
    except Exception:
        if oldest:
            q.appendleft({
                "role":    "system",
                "content": f"[HISTORY COMPRESSED: {len(oldest)} turns omitted]",
                "ts":      oldest[0].get("ts", ""),
            })


def _clear_history(cid: str):
    with _conv_lock:
        _conv_memory.pop(cid, None)
    _cache_clear(cid)  # P1-06: also clear tool result cache
    try:
        sb_patch("telegram_conversations", f"chat_id=eq.{cid}", {"deleted": True})
    except Exception:
        pass
    _invalidate_cache(cid)


def _history_to_text(history: list) -> str:
    lines = []
    for h in history[-MAX_HISTORY_TURNS:]:
        role = h.get("role", "user")
        limit = 600 if role == "assistant" else 200
        content = h.get("content", "")[:limit]
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# METACOGNITIVE LAYER
# ══════════════════════════════════════════════════════════════════════════════

def _reason_before_execute(user_message: str, system_prompt: str,
                            history_text: str, tools_desc: str,
                            trivial: bool = False) -> dict:
    """
    Pre-execution reasoning pass with active 4-brain query.
    P1-03: If trivial=True, skip all Supabase brain fetches and return minimal pre-flight.
    """
    # ── P1-03: Fast path for trivial messages ─────────────────────────────────
    if trivial:
        print(f"[ORCH] P1-03 trivial fast-path — skipping brain query")
        with _metrics_lock:
            _metrics["trivial_fast_path"] += 1
        try:
            raw = _or_text(
                system=(
                    f"{system_prompt}\n\n"
                    "Quick pre-flight: identify intent and whether you can answer directly.\n"
                    "Output ONLY valid JSON:\n"
                    "{\n"
                    '  "intent": "true goal",\n'
                    '  "can_answer_directly": true/false,\n'
                    '  "direct_answer": "full answer if can_answer_directly=true, else empty string",\n'
                    '  "plan": ["step 1"],\n'
                    '  "creative_path": "",\n'
                    '  "fallback_strategy": ""\n'
                    "}"
                ),
                user=f"OWNER MESSAGE: {user_message}",
                max_tokens=400,
                json_mode=True,
            )
            parsed = json.loads(_strip_json(raw))
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            print(f"[ORCH] trivial pre-flight failed (non-fatal): {e}")
            return {}

    # ── Standard path: full brain query ───────────────────────────────────────
    brain_context = ""
    try:
        from core_tools import TOOLS

        # Brain 1: KB
        kb_fn = TOOLS.get("search_kb", {}).get("fn")
        if kb_fn:
            kb = kb_fn(query=user_message[:100], limit="5")
            kb_list = kb if isinstance(kb, list) else kb.get("results", []) if isinstance(kb, dict) else []
            if kb_list:
                brain_context += "KB CONTEXT:\n" + "\n".join(
                    f"  [{r.get('topic','')}] {r.get('content','')[:150]}"
                    for r in kb_list[:5]
                ) + "\n"

        # Brain 2: Mistakes
        m_fn = TOOLS.get("get_mistakes", {}).get("fn")
        if m_fn:
            m = m_fn(limit="3")
            m_list = m if isinstance(m, list) else m.get("mistakes", []) if isinstance(m, dict) else []
            if m_list:
                brain_context += "RELEVANT MISTAKES:\n" + "\n".join(
                    f"  {x.get('what_failed','')[:100]} → {x.get('correct_approach','')[:100]}"
                    for x in m_list[:3]
                ) + "\n"

        # Brain 3: Behavioral rules
        br_fn = TOOLS.get("get_behavioral_rules", {}).get("fn")
        if br_fn:
            br = br_fn(domain="universal", page="1", page_size="5")
            if br.get("ok") and br.get("rules"):
                brain_context += "BEHAVIORAL RULES:\n" + "\n".join(
                    f"  [{r.get('trigger','')}] {r.get('pointer','')[:100]}"
                    for r in br["rules"][:5]
                ) + "\n"

        # Brain 4: Tool discovery
        lt_fn = TOOLS.get("list_tools", {}).get("fn")
        if lt_fn:
            lt = lt_fn(search=user_message[:50])
            if lt.get("ok") and lt.get("tools"):
                brain_context += "RELEVANT TOOLS:\n" + "\n".join(
                    f"  {t['name']}({t['args']}) — {t['desc'][:80]}"
                    for t in lt["tools"][:8]
                ) + "\n"

        # Brain 5: Meta/self-knowledge
        meta_keywords = ["pattern", "failure", "mistake", "learn", "improve",
                         "session", "history", "trend", "reflect", "stale",
                         "outdated", "know", "aware", "self"]
        is_meta = any(kw in user_message.lower() for kw in meta_keywords)
        if is_meta:
            pf_result = sb_get(
                "pattern_frequency",
                f"select={_sel('pattern_frequency')}&id=gt.1&order=frequency.desc&limit=8",
            )
            if pf_result:
                brain_context += "TOP PATTERNS (from training):\n" + "\n".join(
                    "  [" + r.get("domain","?") + "/" + str(r.get("frequency",0)) + "x] " + (r.get("pattern_key") or r.get("pattern",""))[:120]
                    for r in pf_result[:8]
                ) + "\n"
            cr_result = sb_get(
                "cold_reflections",
                f"select={_sel('cold_reflections')}&id=gt.1&order=created_at.desc&limit=5",
            )
            if cr_result:
                summary_field = "summary_text" if _has_col("cold_reflections", "summary_text") else "summary"
                brain_context += "RECENT REFLECTIONS:\n" + "\n".join(
                    "  [" + r.get("domain","?") + "]  " + r.get(summary_field,"")[:120]
                    for r in cr_result[:5]
                ) + "\n"
            mk_result = sb_get(
                "mistakes",
                f"select={_sel('mistakes')}&id=gt.1&order=created_at.desc&limit=5",
            )
            if mk_result:
                fix_field = "correct_approach" if _has_col("mistakes", "correct_approach") else "fix"
                brain_context += "RECENT MISTAKES:\n" + "\n".join(
                    "  [" + r.get("domain","?") + "]  " + r.get("what_failed","")[:80] + " → " + r.get(fix_field,"")[:80]
                    for r in mk_result[:5]
                ) + "\n"

        # Brain 6: Accountability
        accountability_keywords = ["why", "deviat", "didn't", "did not", "fail", "past",
                                    "previous", "log", "internal", "reason", "explain",
                                    "not doing", "not follow", "supposed to"]
        is_accountability = any(kw in user_message.lower() for kw in accountability_keywords)
        if is_accountability:
            sess_result = sb_get(
                "sessions",
                f"select={_sel('sessions')}&id=gt.1&order=created_at.desc&limit=3",
            )
            if sess_result:
                q_field = "quality_score" if _has_col("sessions", "quality_score") else "quality"
                brain_context += "RECENT SESSIONS:\n" + "\n".join(
                    "  [" + r.get("domain","?") + "/q=" + str(r.get(q_field,"?")) + "] " + r.get("summary","")[:150]
                    for r in sess_result[:3]
                ) + "\n"
            hr_result = sb_get(
                "hot_reflections",
                f"select={_sel('hot_reflections')}&id=gt.1&processed_by_cold=eq.false&order=created_at.desc&limit=5",
            )
            if hr_result:
                refl_field = "reflection_text" if _has_col("hot_reflections", "reflection_text") else "reflection"
                brain_context += "UNPROCESSED REFLECTIONS:\n" + "\n".join(
                    "  [" + r.get("domain","?") + "] " + r.get(refl_field,"")[:120]
                    for r in hr_result[:5]
                ) + "\n"

    except Exception as e:
        print(f"[ORCH] brain query failed (non-fatal): {e}")

    prompt = (
        f"{system_prompt}\n\n"
        f"RECENT CONVERSATION (last {MAX_HISTORY_TURNS} turns):\n{history_text}\n\n"
        f"BRAIN QUERY RESULTS (pre-loaded context):\n{brain_context}\n"
        "PRE-FLIGHT REASONING:\n"
        "Identify the true intent. Find the BEST path — not just the obvious one.\n"
        "Ask: can run_python solve this directly? What tool combination works here?\n"
        "Only label claims CONFIRMED/INFERRED/UNKNOWN when precision is critical.\n"
        "Output ONLY valid JSON:\n"
        "{\n"
        '  "intent": "true goal behind the message — what does owner actually want?",\n'
        '  "can_answer_directly": true/false,\n'
        '  "direct_answer": "full answer if can_answer_directly=true, else empty string",\n'
        '  "plan": ["concrete step 1", "concrete step 2", ...],\n'
        '  "creative_path": "non-obvious solution using run_python or tool combinations — empty string if standard path is best",\n'
        '  "fallback_strategy": "specific alternative if primary approach fails"\n'
        "}"
    )
    try:
        raw = _or_text(
            system=prompt,
            user=f"OWNER MESSAGE: {user_message}",
            max_tokens=800,
            json_mode=True,
        )
        parsed = json.loads(_strip_json(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"[ORCH] _reason_before_execute failed (non-fatal): {e}")
        return {}


def _validate_before_reply(user_message: str, reply: str,
                            results_buffer: list, system_prompt: str) -> dict:
    """Post-execution self-check."""
    results_summary = "\n".join(
        f"[{r['name']}] → {r['result'][:400]}" for r in results_buffer[-10:]
    ) or "No tools called."

    prompt = (
        "You are a quality checker for CORE AGI.\n"
        "Evaluate the draft reply on TWO dimensions:\n"
        "1. CORRECTNESS: does it actually answer the owner's question?\n"
        "2. OPTIMALITY: was the approach efficient? Could run_python or a simpler path have worked better?\n\n"
        f"OWNER QUESTION: {user_message}\n\n"
        f"TOOL RESULTS AVAILABLE:\n{results_summary}\n\n"
        f"DRAFT REPLY: {reply or '(empty)'}\n\n"
        "Output ONLY valid JSON:\n"
        "{\n"
        '  "is_valid": true/false,\n'
        '  "reason": "why correct/incorrect and optimal/suboptimal",\n'
        '  "corrected_reply": "improved reply using available tool results — empty string if reply is already good",\n'
        '  "better_approach": "if suboptimal, describe smarter path for next time — empty string if approach was fine"\n'
        "}"
    )
    try:
        raw = _or_text(
            system=prompt,
            user="Validate.",
            max_tokens=600,
            json_mode=True,
        )
        parsed = json.loads(_strip_json(raw))
        return parsed if isinstance(parsed, dict) else {"is_valid": True}
    except Exception as e:
        print(f"[ORCH] _validate_before_reply failed (non-fatal): {e}")
        return {"is_valid": True}


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ABSTRACTION LAYER — main reasoning call
# ══════════════════════════════════════════════════════════════════════════════

def _build_reasoning_payload(system_prompt: str, history_text: str,
                              user_message: str, tools_desc: str,
                              image_b64: str = None, image_mime: str = "image/jpeg",
                              file_b64: str = None, file_mime: str = None,
                              pre_flight: dict = None) -> tuple:
    """Build system string and user content list for reasoning call."""
    plan_hint = ""
    if pre_flight and pre_flight.get("plan"):
        plan_hint = "\nEXECUTION PLAN:\n" + "\n".join(
            f"  {i+1}. {s}" for i, s in enumerate(pre_flight["plan"])
        )
    if pre_flight and pre_flight.get("fallback_strategy"):
        plan_hint += f"\nFALLBACK: {pre_flight['fallback_strategy']}"
    if pre_flight and pre_flight.get("known_context"):
        plan_hint += f"\nKNOWN CONTEXT: {pre_flight['known_context']}"

    creative_hint = ""
    if pre_flight and pre_flight.get("creative_path"):
        creative_hint = f"\nCREATIVE PATH IDENTIFIED: {pre_flight['creative_path']}"

    full_system = (
        f"{system_prompt}\n\n"
        f"AVAILABLE TOOLS:\n{tools_desc}\n\n"
        f"CONVERSATION SO FAR:\n{history_text}"
        f"{plan_hint}{creative_hint}\n\n"
        "CREATIVE EXECUTION PATHS — try these before giving up:\n"
        "• No HTTP/API tool? → run_python with requests: works for Telegram Bot API, GitHub API, any REST endpoint\n"
        "• Need to process data? → run_python: parse JSON/CSV/HTML, compute, transform\n"
        "• Need to automate on PC? → desktop_run_script(PowerShell or Python)\n"
        "• Need web info? → web_fetch or desktop_search_web\n"
        "• Complex multi-step? → chain tools: fetch → process with run_python → store → notify\n"
        "• Always ask: what does run_python + {any API} enable here?\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"thought": "your actual reasoning — what is the real goal, what is the best approach, why this tool/combination", '
        '"tool_calls": [{"name": "tool_name", "args": {}}], '
        '"reply": "direct answer to owner when done — concrete, no fluff", '
        '"done": true/false}\n'
        "Rules:\n"
        "- thought: use this — explain WHY this approach, not just WHAT\n"
        "- done=true ONLY when task fully complete AND reply non-empty\n"
        "- tool_calls=[] only when replying directly with zero execution needed\n"
        "- Never invent tool results — call the tool\n"
        "- Tool fails or empty → try alternative immediately. search_kb miss → sb_query direct. run_python can always be the alternative.\n"
        "- For KB/brain queries: search_kb → sb_query(mistakes/pattern_frequency/cold_reflections/sessions)\n"
        "- Output ONLY valid JSON, no markdown fences"
    )

    user_content: list = []
    if image_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
        })
    if file_b64 and file_mime:
        if file_mime.startswith("image/"):
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{file_mime};base64,{file_b64}"},
            })
        elif file_mime == "application/pdf":
            user_content.append({
                "type": "text",
                "text": f"[FILE ATTACHED: PDF document, base64 encoded, {len(file_b64)} chars. Use read_document or run_python to extract text if needed.]",
            })
        else:
            user_content.append({
                "type": "text",
                "text": f"[FILE ATTACHED: {file_mime}, base64 encoded, {len(file_b64)} chars. Use appropriate tool to process.]",
            })
    user_content.append({"type": "text", "text": f"OWNER: {user_message}"})

    return full_system, user_content


def _call_model(system_prompt: str, history_text: str, user_message: str,
                tools_desc: str, image_b64: str = None,
                image_mime: str = "image/jpeg",
                file_b64: str = None, file_mime: str = None,
                pre_flight: dict = None) -> dict:
    """Main reasoning call. Chain: OpenRouter → Gemini → Groq."""
    full_system, user_content = _build_reasoning_payload(
        system_prompt, history_text, user_message, tools_desc,
        image_b64, image_mime, file_b64, file_mime, pre_flight,
    )
    errors = []

    # 1. OpenRouter
    try:
        payload = {
            "model":           OPENROUTER_MODEL,
            "max_tokens":      2048,
            "temperature":     0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": full_system},
                {"role": "user",   "content": user_content},
            ],
        }
        data     = _or_call(payload, timeout=60)
        choice   = data["choices"][0]
        finish   = choice.get("finish_reason", "")
        raw_text = choice["message"].get("content") or "{}"
        if finish == "length":
            print(f"[ORCH] WARNING: model output truncated (finish_reason=length)")
            raw_text = raw_text.rstrip().rstrip(",") + "}"
        try:
            parsed = json.loads(_strip_json(raw_text))
        except Exception:
            return {"thought": "", "tool_calls": [], "reply": raw_text, "done": True}
        return {
            "thought":    parsed.get("thought", ""),
            "tool_calls": parsed.get("tool_calls", []),
            "reply":      parsed.get("reply", ""),
            "done":       bool(parsed.get("done", False)),
        }
    except Exception as e:
        errors.append(f"OpenRouter: {str(e)[:200]}")
        print(f"[ORCH] OpenRouter reasoning failed, trying Gemini: {str(e)[:200]}")

    # 2. Gemini direct
    try:
        combined_prompt = f"{full_system}\n\nOWNER: {user_message}"
        if image_b64:
            from core_config import _GEMINI_KEYS
            import core_config as _cc
            keys = _cc._GEMINI_KEYS
            if not keys:
                raise RuntimeError("GEMINI_KEYS not set")
            parts_list = [
                {"text": combined_prompt},
                {"inline_data": {"mime_type": image_mime, "data": image_b64}},
            ]
            last_err = None
            for _ in range(len(keys)):
                key = keys[_cc._GEMINI_KEY_INDEX % len(keys)]
                _cc._GEMINI_KEY_INDEX = (_cc._GEMINI_KEY_INDEX + 1) % len(keys)
                try:
                    r = httpx.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{GEMINI_FALLBACK_MODEL}:generateContent",
                        params={"key": key},
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{"parts": parts_list}],
                            "generationConfig": {
                                "maxOutputTokens": 2048, "temperature": 0.1,
                                "responseMimeType": "application/json",
                            },
                            "safetySettings": [
                                {"category": c, "threshold": "BLOCK_NONE"}
                                for c in [
                                    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                                    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
                                ]
                            ],
                        },
                        timeout=30,
                    )
                    if r.status_code == 429:
                        last_err = "429"
                        continue
                    r.raise_for_status()
                    candidate  = r.json().get("candidates", [{}])[0]
                    resp_parts = candidate.get("content", {}).get("parts", [])
                    if not resp_parts:
                        last_err = "empty parts"
                        continue
                    raw = resp_parts[0]["text"].strip()
                    parsed = json.loads(_strip_json(raw))
                    return {
                        "thought":    parsed.get("thought", ""),
                        "tool_calls": parsed.get("tool_calls", []),
                        "reply":      parsed.get("reply", ""),
                        "done":       bool(parsed.get("done", False)),
                    }
                except Exception as ex:
                    last_err = str(ex)
                    continue
            raise RuntimeError(f"Gemini image all keys failed: {last_err}")
        else:
            raw = gemini_chat(
                system=full_system,
                user=f"OWNER: {user_message}",
                max_tokens=2048,
                json_mode=True,
            )
            parsed = json.loads(_strip_json(raw))
            return {
                "thought":    parsed.get("thought", ""),
                "tool_calls": parsed.get("tool_calls", []),
                "reply":      parsed.get("reply", ""),
                "done":       bool(parsed.get("done", False)),
            }
    except Exception as e:
        errors.append(f"Gemini: {str(e)[:200]}")
        print(f"[ORCH] Gemini reasoning failed, trying Groq: {str(e)[:200]}")

    # 3. Groq last resort
    try:
        from core_config import groq_chat
        full_prompt = (
            f"{full_system}\n\n"
            "Output ONLY valid JSON, no markdown fences."
        )
        raw = groq_chat(
            system=full_prompt,
            user=f"OWNER: {user_message}",
            model=GROQ_LAST_RESORT_MODEL,
            max_tokens=1024,
        )
        try:
            parsed = json.loads(_strip_json(raw))
        except Exception:
            return {"thought": "", "tool_calls": [], "reply": raw, "done": True}
        return {
            "thought":    parsed.get("thought", ""),
            "tool_calls": parsed.get("tool_calls", []),
            "reply":      parsed.get("reply", ""),
            "done":       bool(parsed.get("done", False)),
        }
    except Exception as e:
        errors.append(f"Groq: {str(e)[:200]}")

    raise RuntimeError("All providers failed:\n" + "\n".join(errors))


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS DESCRIPTION
# ══════════════════════════════════════════════════════════════════════════════

def _build_tools_desc(selected_tool_names: list) -> str:
    lines = []
    total_chars = 0
    TOTAL_DESC_BUDGET = 12000
    try:
        from core_tools import TOOLS
        for name in selected_tool_names:
            tdef = TOOLS.get(name)
            if not tdef:
                continue
            args_str = ", ".join(
                (a["name"] if isinstance(a, dict) else a)
                for a in (tdef.get("args") or [])
            )
            desc = tdef.get("desc", "")[:300]
            line = f"  {name}({args_str}) — {desc}"
            lines.append(line)
            total_chars += len(line)
            if total_chars >= TOTAL_DESC_BUDGET:
                remaining = len(selected_tool_names) - len(lines)
                if remaining > 0:
                    lines.append(f"  ... ({remaining} more tools available — call list_tools to discover)")
                break
    except Exception as e:
        print(f"[ORCH] _build_tools_desc failed: {e}")
    lines += [
        "  desktop_run_script(script, lang) — run PowerShell/Python on PC",
        "  desktop_file_ops(path, operation, content?) — read/write/list/delete/move/mkdir/info",
        "  desktop_browser(url?, steps, screenshot?) — Puppeteer browser on PC",
        "  desktop_search_web(query, max_results?) — web search from PC",
        "  desktop_cmd(command?, script?) — shell command on PC",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _compress_result(result_str: str, tool_name: str) -> str:
    if tool_name in ("list_tools", "get_tool_info", "get_behavioral_rules"):
        return result_str
    limit = MAX_TOOL_RESULT_CHARS
    if len(result_str) <= limit:
        return result_str
    try:
        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            compressed = {}
            for k, v in parsed.items():
                if k in ("ok", "error", "error_code", "message", "status",
                         "count", "total", "applied", "inserted", "summary",
                         "result", "output", "commit", "path", "version"):
                    compressed[k] = v
                elif isinstance(v, list):
                    compressed[f"{k}_count"] = len(v)
                    if k in ("results", "items", "hits", "entries", "tools", "rules", "mistakes"):
                        compressed[k] = v[:20]
                    elif v and len(str(v[0])) < 200:
                        compressed[f"{k}_first"] = v[0]
                else:
                    compressed[k] = v
            out = json.dumps(compressed, default=str)
            if len(out) <= limit:
                return out
    except Exception:
        pass
    return result_str[:limit] + "…[truncated]"


def _execute_railway_tool(tool_name: str, tool_args: dict, cid: str = "") -> str:
    """Execute a Railway-side tool. P1-06: checks cache for READ tools first."""
    # P1-06: READ tool cache check
    if cid and _is_cacheable_tool(tool_name):
        cached = _cache_get(cid, tool_name, tool_args)
        if cached is not None:
            print(f"[ORCH] P1-06 cache HIT: {tool_name}")
            with _metrics_lock:
                _metrics["cache_hits"] += 1
            return cached

    try:
        from core_tools import TOOLS
        if tool_name not in TOOLS:
            err = f"tool '{tool_name}' not found in TOOLS registry ({len(TOOLS)} tools available)"
            print(f"[ORCH] {err}")
            return json.dumps({"ok": False, "error": err})
        fn = TOOLS[tool_name]["fn"]
        if tool_args and len(tool_args) == 1 and "args" in tool_args and isinstance(tool_args["args"], dict):
            tool_args = tool_args["args"]
        print(f"[ORCH] {tool_name} args: {json.dumps(tool_args, default=str)[:200]}")
        try:
            result = fn(**tool_args) if tool_args else fn()
        except Exception as inner_e:
            print(f"[ORCH] {tool_name} INNER ERROR: {type(inner_e).__name__}: {str(inner_e)[:300]}")
            raise
        raw    = json.dumps(result, default=str)
        compressed = _compress_result(raw, tool_name)
        print(f"[ORCH] {tool_name} → {len(raw)}b raw, {len(compressed)}b compressed")

        # P1-06: Store in cache if cacheable
        if cid and _is_cacheable_tool(tool_name):
            _cache_set(cid, tool_name, tool_args, compressed)

        return compressed
    except TypeError as e:
        err = str(e)
        hint = f"Wrong args for {tool_name}: {err}. Check get_tool_info(name='{tool_name}') for correct params."
        print(f"[ORCH] {tool_name} TypeError: {err}")
        return json.dumps({"ok": False, "error": hint, "fix": f"call get_tool_info(name='{tool_name}') to verify args"})
    except Exception:
        err = traceback.format_exc()[:400]
        print(f"[ORCH] {tool_name} EXCEPTION: {err[:200]}")
        return json.dumps({"ok": False, "error": err, "fix": f"check get_tool_info(name='{tool_name}') or try alternative tool"})


def _execute_desktop_tool(tool_name: str, tool_args: dict, cid: str) -> str:
    action = tool_name.replace("desktop_", "", 1)
    if action == "browser":
        if isinstance(tool_args.get("steps"), str):
            try:
                tool_args["steps"] = json.loads(tool_args["steps"])
            except Exception:
                pass
        if isinstance(tool_args.get("screenshot"), str):
            tool_args["screenshot"] = tool_args["screenshot"].lower() == "true"
    try:
        task_payload = json.dumps({
            "desktop_agent": True,
            "action":        action,
            "payload":       tool_args,
            "chat_id":       cid,
            "queued_at":     datetime.utcnow().isoformat(),
        })
        ok = sb_post("task_queue", {
            "task":     task_payload,
            "status":   "pending",
            "priority": 9,
            "source":   "mcp_session",
            "chat_id":  cid,
        })
        if not ok:
            return json.dumps({"ok": False, "error": "sb_post failed for task_queue"})
        rows = sb_get(
            "task_queue",
            f"select=id&source=eq.mcp_session&status=eq.pending"
            f"&chat_id=eq.{cid}&order=created_at.desc&limit=1",
        ) or []
        if not rows:
            return json.dumps({"ok": False, "error": "task queued but id not found"})
        task_id  = str(rows[0]["id"])
        deadline = time.time() + DESKTOP_TASK_TIMEOUT
        while time.time() < deadline:
            r = sb_get(
                "task_queue",
                f"select={_sel_force('task_queue', ['id','status','result','error'])}&id=eq.{task_id}&limit=1",
            ) or []
            if r:
                status = r[0].get("status")
                if status == "done":
                    return _compress_result(r[0].get("result") or "Done.", tool_name)
                elif status == "failed":
                    return json.dumps({"ok": False, "error": r[0].get("error", "unknown")})
            time.sleep(5)
        return json.dumps({"ok": False, "error": f"desktop task timed out after {DESKTOP_TASK_TIMEOUT}s"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# CONFIRMATION GATE
# ══════════════════════════════════════════════════════════════════════════════

_DESTRUCTIVE_KW = {
    "drop", "format", "wipe", "truncate", "purge",
    "rm -rf", "destroy", "sb_delete", "delete", "permanent", "irreversible",
}


def _is_destructive(tool_name: str, tool_args: dict) -> bool:
    tn = tool_name.lower()
    if any(tn == kw or tn.startswith(kw + "_") or tn.endswith("_" + kw)
           for kw in _DESTRUCTIVE_KW):
        return True
    args_str = json.dumps(tool_args, default=str).lower()
    for kw in _DESTRUCTIVE_KW:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, args_str):
            return True
    return False


def handle_confirm_reply(cid: str, text: str) -> bool:
    with _confirm_lock:
        gate = _pending_confirms.get(cid)
        if not gate:
            return False
        upper = text.strip().upper()
        if upper in ("CONFIRM", "YES", "Y", "OK", "GO", "DO IT", "PROCEED"):
            gate["confirmed"] = True
        elif upper in ("REJECT", "CANCEL", "NO", "N", "STOP", "ABORT", "SKIP"):
            gate["confirmed"] = False
        else:
            return False
        gate["event"].set()
        return True


def _request_confirmation(cid: str, tool_name: str, tool_args: dict) -> bool:
    event = threading.Event()
    with _confirm_lock:
        _pending_confirms[cid] = {"confirmed": False, "event": event}
    preview = json.dumps(tool_args, default=str)[:300]
    _tg_send(
        cid,
        f"⚠️ <b>CONFIRMATION REQUIRED</b>\n\n"
        f"Tool: <code>{tool_name}</code>\n"
        f"Args: <code>{preview}</code>\n\n"
        f"Reply <b>CONFIRM</b> to proceed or <b>REJECT</b> to cancel.\n"
        f"(Timeout: {CONFIRM_TIMEOUT_SECS}s)"
    )
    fired = event.wait(timeout=CONFIRM_TIMEOUT_SECS)
    with _confirm_lock:
        gate      = _pending_confirms.pop(cid, {})
        confirmed = gate.get("confirmed", False)
    if not fired:
        _tg_send(cid, "⏱ Timed out — action cancelled.")
        return False
    return confirmed


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _tg_send(cid: str, text: str):
    try:
        chunk_size = 4000
        if len(text) <= chunk_size:
            notify(text, cid=cid)
        else:
            chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            for chunk in chunks:
                notify(chunk, cid=cid)
                time.sleep(0.3)
    except Exception as e:
        print(f"[ORCH] _tg_send error: {e}")


def _tg_typing(cid: str):
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            data={"chat_id": cid, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass


def _tg_photo(cid: str, image_b64: str, caption: str = ""):
    try:
        import io
        img_bytes = base64.b64decode(image_b64)
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": cid, "caption": caption[:1024]},
            files={"photo": ("screenshot.png", io.BytesIO(img_bytes), "image/png")},
            timeout=30,
        )
    except Exception as e:
        print(f"[ORCH] _tg_photo error: {e}")


def _tg_download_file(file_id: str) -> Optional[str]:
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        img_r = httpx.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=30,
        )
        img_r.raise_for_status()
        return base64.b64encode(img_r.content).decode()
    except Exception as e:
        print(f"[ORCH] _tg_download_file error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AGENTIC LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _safe_result(r: str, tool_name: str = "") -> str:
    try:
        parsed = json.loads(r)
        if isinstance(parsed, dict):
            if parsed.get("ok") is False or parsed.get("error"):
                return f"TOOL_FAILED: {json.dumps(parsed, default=str)[:300]}"
            if tool_name in ("search_kb", "search_mistakes", "ask"):
                results = parsed.get("results") or parsed.get("items") or []
                if not results:
                    return (
                        "KB_EMPTY: no results. Try: different keywords, "
                        "sb_query direct tables, web_search, or synthesize from context."
                    )
    except Exception:
        pass
    return r


def _log_hot_reflection(user_message: str, results_buffer: list, reply: str):
    """Log hot_reflection after every completed agentic loop. Runs in background thread."""
    tools_used = [r["name"] for r in results_buffer]
    tool_count = len(tools_used)
    failed     = sum(1 for r in results_buffer if _safe_result(r["result"], r["name"]).startswith("TOOL_FAILED"))
    quality    = max(0.3, 0.9 - (failed * 0.1))
    payload = {
        "domain":          "core_agi",
        "task_summary":    f"Telegram: {user_message[:300]}",
        "quality_score":   quality,
        "reflection_text": (
            f"Completed via Telegram orchestrator. "
            f"Tools called: {tool_count} ({', '.join(tools_used[:10])}). "
            f"Failures: {failed}. "
            f"Reply preview: {reply[:300]}"
        ),
        "source": "core_orchestrator",
        "processed_by_cold": False,
    }
    try:
        from core_tools import TOOLS as _T
        for name in ("add_hot_reflection", "log_hot_reflection", "write_hot_reflection"):
            fn = _T.get(name, {}).get("fn")
            if fn:
                fn(**{k: v for k, v in payload.items()
                      if k in ("domain", "task_summary", "quality_score", "reflection_text", "source")})
                return
    except Exception:
        pass
    try:
        sb_post("hot_reflections", payload)
    except Exception as e:
        print(f"[ORCH] _log_hot_reflection failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# P3-03: AUTONOMOUS MODE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _is_autonomous_trigger(message: str) -> bool:
    """Return True if message contains explicit multi-step intent patterns."""
    for pattern in _AUTONOMOUS_TRIGGER_RE:
        if pattern.search(message):
            return True
    return False


def _all_steps_safe(tool_calls: list) -> bool:
    """Return True if all planned tool calls are non-destructive (READ or reversible WRITE).
    Used to gate autonomous mode — irreversible tools always require confirmation."""
    try:
        from core_tools import TOOLS
        for tc in tool_calls:
            name = tc.get("name", "")
            perm = TOOLS.get(name, {}).get("perm", "READ")
            if perm == "EXECUTE":
                # EXECUTE is ok if not destructive
                if _is_destructive(name, tc.get("args") or {}):
                    return False
            # WRITE is always ok in autonomous mode (reversible)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# P3-05: IMPLICIT FEEDBACK DETECTION
# Detects correction signals in follow-up messages to score response quality.
# ══════════════════════════════════════════════════════════════════════════════

_NEGATIVE_SIGNALS = [
    r'\bwrong\b', r'\bsalah\b', r'\bfix\s+it\b', r'\bfix\s+that\b',
    r'\bthat\'?s\s+not\s+right\b', r'\bno[,\s]', r'\bbukan\b',
    r'\btidak\b', r'\bkeliru\b', r'\bredo\b', r'\btry\s+again\b',
    r'\bretry\b', r'\bincorrect\b', r'\bnot\s+what\s+i\b',
    r'\bitu\s+salah\b', r'\bbukan\s+itu\b',
]
_NEGATIVE_RE = [re.compile(p, re.IGNORECASE) for p in _NEGATIVE_SIGNALS]

_POSITIVE_SIGNALS = [
    r'\bgood\b', r'\bok\b', r'\bokay\b', r'\bnice\b', r'\bperfect\b',
    r'\bthanks?\b', r'\bterima\s+kasih\b', r'\bbagus\b', r'\bbenar\b',
    r'\bcorrect\b', r'\bgreat\b', r'\bdone\b', r'\byes\b',
]
_POSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _POSITIVE_SIGNALS]


def _record_reply(cid: str, user_message: str, reply: str):
    """Record the last reply for P3-05 follow-up detection."""
    with _last_reply_lock:
        _last_reply[cid] = {
            "ts":      time.time(),
            "message": user_message[:200],
            "reply":   reply[:200],
        }


def _detect_implicit_feedback(cid: str, incoming_message: str) -> str:
    """Check if incoming_message is a correction/confirmation of last reply.
    Returns: 'positive' | 'negative' | 'neutral'
    Fires a background hot_reflection micro-signal if signal detected.
    """
    with _last_reply_lock:
        last = _last_reply.get(cid)
    if not last:
        return "neutral"

    elapsed = time.time() - last["ts"]
    # Only relevant if follow-up within 3 minutes
    if elapsed > 180:
        return "neutral"

    msg_lower = incoming_message.strip().lower()

    # Short messages only — long messages are new requests, not feedback
    if len(msg_lower) > 80:
        return "neutral"

    is_negative = any(p.search(msg_lower) for p in _NEGATIVE_RE)
    is_positive = any(p.search(msg_lower) for p in _POSITIVE_RE)

    signal = "neutral"
    if is_negative and not is_positive:
        signal = "negative"
    elif is_positive and not is_negative:
        signal = "positive"

    if signal != "neutral":
        # Fire micro-quality signal to hot_reflections (background, non-blocking)
        def _fire():
            try:
                quality = 0.9 if signal == "positive" else 0.3
                sb_post("hot_reflections", {
                    "domain":          "core_agi",
                    "task_summary":    f"[P3-05] implicit_{signal}: {last['message'][:100]}",
                    "quality_score":   quality,
                    "reflection_text": (
                        f"Implicit {signal} feedback detected. "
                        f"Last reply: {last['reply'][:150]}. "
                        f"Follow-up: {incoming_message[:100]}. "
                        f"Elapsed: {int(elapsed)}s."
                    ),
                    "source":              "implicit_feedback",
                    "processed_by_cold":   False,
                })
                print(f"[P3-05] implicit_{signal} signal fired for {cid}")
            except Exception as e:
                print(f"[P3-05] fire error: {e}")
        threading.Thread(target=_fire, daemon=True).start()

        with _metrics_lock:
            if signal == "positive":
                _metrics["implicit_positive"] += 1
            else:
                _metrics["implicit_negative"] += 1

    return signal


# ══════════════════════════════════════════════════════════════════════════════
# P3-06: PREDICTIVE CONTEXT PRE-LOADER
# Pre-warms context cache based on time-of-day + recent session patterns.
# ══════════════════════════════════════════════════════════════════════════════

def _get_predictive_context(cid: str) -> str:
    """Return pre-warmed context string if available and fresh. Else empty."""
    with _predictive_cache_lock:
        entry = _predictive_cache.get(cid)
        if not entry:
            return ""
        if time.time() - entry["loaded_at"] > _PREDICTIVE_CACHE_TTL:
            del _predictive_cache[cid]
            return ""
        with _metrics_lock:
            _metrics["predictive_hits"] += 1
        print(f"[P3-06] predictive cache HIT for {cid}: pattern={entry.get('pattern_key','?')}")
        return entry.get("context_text", "")


def _run_predictive_loader():
    """P3-06 background thread: predict + pre-warm context every 30 minutes.
    Uses time-of-day + day-of-week + active goals + recent session patterns
    to build a relevant context block before the owner sends a message.
    """
    from zoneinfo import ZoneInfo
    WIB = ZoneInfo("Asia/Jakarta")

    while True:
        try:
            time.sleep(_PREDICTIVE_CHECK_INTERVAL)
            from datetime import datetime as _dt
            now_wib   = _dt.now(WIB)
            hour_wib  = now_wib.hour
            weekday   = now_wib.weekday()   # 0=Mon … 6=Sun
            cid       = str(TELEGRAM_CHAT)

            # Determine likely intent pattern
            pattern_key  = "general"
            preload_hints = []

            # Sunday evening (17-23 WIB) → likely LSEI / project work
            if weekday == 6 and 17 <= hour_wib <= 23:
                pattern_key   = "lsei_project"
                preload_hints = ["LSEI", "RMU", "MLTX", "commissioning", "project"]

            # Weekday morning (07-10 WIB) → task queue review
            elif weekday < 5 and 7 <= hour_wib <= 10:
                pattern_key   = "task_review"
                preload_hints = ["task", "pending", "queue"]

            # Late night any day (22-02 WIB) → CORE development
            elif hour_wib >= 22 or hour_wib <= 2:
                pattern_key   = "core_dev"
                preload_hints = ["core_agi", "railway", "deploy", "patch"]

            # Check if cache already fresh for this pattern
            with _predictive_cache_lock:
                existing = _predictive_cache.get(cid, {})
                if (existing.get("pattern_key") == pattern_key and
                        time.time() - existing.get("loaded_at", 0) < _PREDICTIVE_CACHE_TTL):
                    continue  # still fresh

            # Build predictive context
            parts = [f"PREDICTIVE CONTEXT (pattern: {pattern_key}, {now_wib.strftime('%a %H:%M WIB')}):"]

            try:
                # Active goals
                active_goals = sb_get(
                    "session_goals",
                    "select=goal,domain,progress&status=eq.active&order=updated_at.desc&limit=5",
                    svc=True,
                ) or []
                if active_goals:
                    goal_lines = " | ".join(g.get("goal", "")[:80] for g in active_goals[:3])
                    parts.append(f"Active goals: {goal_lines}")
            except Exception:
                pass

            if preload_hints:
                try:
                    # KB entries matching likely domain
                    kw = preload_hints[0]
                    kb_rows = sb_get(
                        "knowledge_base",
                        f"select=topic,instruction&active=eq.true&id=gt.1"
                        f"&or=(topic.ilike.*{kw}*,instruction.ilike.*{kw}*)"
                        f"&order=confidence.desc&limit=6",
                        svc=True,
                    ) or []
                    if kb_rows:
                        kb_text = " | ".join(r.get("topic", "")[:60] for r in kb_rows[:4])
                        parts.append(f"Relevant KB ({kw}): {kb_text}")
                except Exception:
                    pass

                try:
                    # Pending tasks matching pattern
                    kw2 = preload_hints[0].lower()
                    task_rows = sb_get(
                        "task_queue",
                        f"select=task,priority&status=eq.pending"
                        f"&task=ilike.*{kw2}*&order=priority.desc&limit=4",
                        svc=True,
                    ) or []
                    if task_rows:
                        t_text = " | ".join(str(r.get("task",""))[:60] for r in task_rows[:3])
                        parts.append(f"Pending tasks: {t_text}")
                except Exception:
                    pass

            ctx_text = "\n".join(parts)

            with _predictive_cache_lock:
                _predictive_cache[cid] = {
                    "context_text": ctx_text,
                    "loaded_at":    time.time(),
                    "pattern_key":  pattern_key,
                }
            print(f"[P3-06] predictive cache updated: pattern={pattern_key} chars={len(ctx_text)}")

        except Exception as e:
            print(f"[P3-06] predictive loader error: {e}")


def _agentic_loop(cid: str, user_message: str,
                  image_b64: str = None, image_mime: str = "image/jpeg",
                  file_b64: str = None, file_mime: str = None):
    """
    Full agentic loop with metacognitive wrapper.
    P1-03: trivial messages skip brain pre-fetch.
    P1-06: READ tool results cached per session.
    P3-03: autonomous multi-step mode for explicit sequences.
    P3-05: implicit feedback detection wired at reply points.
    P3-06: predictive context injected into system prompt.
    """
    with _metrics_lock: _metrics["total_messages"] += 1

    history       = _get_history(cid)
    history_text  = _history_to_text(history)
    system_prompt = _build_system_prompt(cid, recent_text=user_message)

    # ── P3-06: Inject predictive context if available ─────────────────────────
    pred_ctx = _get_predictive_context(cid)
    if pred_ctx:
        system_prompt = system_prompt + f"\n\n{pred_ctx}"

    # ── P3-03: Detect autonomous mode intent ──────────────────────────────────
    is_autonomous = (
        not image_b64 and not file_b64
        and _is_autonomous_trigger(user_message)
        and not _is_trivial(user_message)
    )
    if is_autonomous:
        print(f"[P3-03] Autonomous mode triggered for: {user_message[:80]}")
        with _metrics_lock: _metrics["autonomous_runs"] += 1

    # ── P1-03: Classify message before pre-flight ──────────────────────────────
    is_trivial = _is_trivial(user_message) and not image_b64 and not file_b64
    print(f"[ORCH] Message classified: {'trivial' if is_trivial else 'standard'} autonomous={is_autonomous}")

    # ── Phase 0: Reason before executing ──────────────────────────────────────
    selected_tools = _select_tools(user_message, history_text)
    tools_desc     = _build_tools_desc(selected_tools)

    pre_flight = _reason_before_execute(
        user_message, system_prompt, history_text, tools_desc,
        trivial=is_trivial,
    )

    # P1-04: Auto-wire predict_failure for write/deploy messages (non-trivial only)
    if not is_trivial:
        _WRITE_KEYWORDS = ["deploy", "patch", "fix", "update", "redeploy", "push",
                           "insert", "delete", "drop", "write", "create", "edit"]
        if any(kw in user_message.lower() for kw in _WRITE_KEYWORDS):
            try:
                from core_tools import TOOLS as _AGI_TOOLS
                _pf_fn = _AGI_TOOLS.get("predict_failure", {}).get("fn")
                if _pf_fn:
                    _pf = _pf_fn(operation=user_message[:80], domain="core_agi")
                    if _pf.get("predicted") and _pf.get("predicted_failure_modes"):
                        top_modes = _pf["predicted_failure_modes"][:2]
                        warn_text = " | ".join(
                            f"{m['mode'][:60]} (p={m['probability']})" for m in top_modes
                        )
                        pre_flight["predict_failure_warning"] = warn_text
                        print(f"[ORCH] P1-04 predict_failure: {warn_text[:120]}")
            except Exception as _pfw_e:
                print(f"[ORCH] predict_failure pre-flight error (non-fatal): {_pfw_e}")

    # ── P3-03: Inject autonomous mode instructions into system prompt ─────────
    if is_autonomous:
        system_prompt = system_prompt + (
            "\n\nAUTONOMOUS MODE ACTIVE: Execute the full multi-step sequence end-to-end. "
            "Do NOT send intermediate status messages. Complete all steps, then return "
            "a single summary: what was done, what worked, what failed, what was skipped. "
            "If any step hits a destructive action, pause and ask for confirmation first."
        )

    # Can answer directly without tools?
    if pre_flight.get("can_answer_directly") and pre_flight.get("direct_answer"):
        direct = pre_flight["direct_answer"].strip()
        if direct:
            check = _validate_before_reply(user_message, direct, [], system_prompt)
            final = (check.get("corrected_reply") or direct) if not check.get("is_valid", True) else direct
            _tg_send(cid, final)
            _append_history(cid, "assistant", final)
            _record_reply(cid, user_message, final)           # P3-05
            with _metrics_lock: _metrics["direct_answers"] += 1
            return

    # ── Phase 1+2: Agentic tool loop ───────────────────────────────────────────
    tool_call_count = 0
    _prev_count     = 0
    results_buffer: list = []
    _seen_calls: set     = set()

    while tool_call_count < MAX_TOOL_CALLS:
        _prev_count = tool_call_count
        if tool_call_count > 0:
            last_tool = results_buffer[-1]["name"] if results_buffer else ""
            if last_tool in ("redeploy", "deploy_and_wait"):
                _sleep = 15
            elif last_tool in ("patch_file", "multi_patch", "gh_search_replace", "write_file"):
                _sleep = 5
            elif last_tool.startswith("desktop_"):
                _sleep = 3
            else:
                _sleep = 1
            time.sleep(_sleep)
        _tg_typing(cid)

        if results_buffer:
            visible  = results_buffer[-8:]
            dropped  = len(results_buffer) - len(visible)
            tool_results_text = "\n".join(
                f"[{r['name']}] → {_safe_result(r['result'], r['name'])}"
                for r in visible
            )
            if dropped:
                tool_results_text = f"[...{dropped} earlier results omitted...]\n" + tool_results_text
            current_user = f"{user_message}\n\nTOOL RESULTS SO FAR:\n{tool_results_text}"
        else:
            current_user = user_message

        failed_results = [
            r for r in results_buffer
            if _safe_result(r["result"], r["name"]).startswith("TOOL_FAILED")
        ]
        if failed_results:
            last_fail = failed_results[-1]
            current_user += (
                f"\n\nTOOL_FAILURE_ALERT: {last_fail['name']} failed — "
                "decide: (1) retry with different args, (2) use alternative tool, "
                "(3) ask owner for clarification. Do NOT repeat the same failing call."
            )
            # P1-04: Auto-wire mid_task_correct on 2+ consecutive failures
            if len(failed_results) >= 2:
                try:
                    from core_tools import TOOLS as _AGI_TOOLS
                    _mtc_fn = _AGI_TOOLS.get("mid_task_correct", {}).get("fn")
                    if _mtc_fn:
                        _mtc = _mtc_fn(
                            anomaly=f"{last_fail['name']} failed: {last_fail['result'][:200]}",
                            last_action=last_fail["name"],
                            last_result=last_fail["result"][:200],
                            task_state=f"{len(results_buffer)} tool calls so far",
                        )
                        if _mtc.get("ok"):
                            current_user += f"\n\nMID_TASK_CORRECTION: {json.dumps(_mtc, default=str)[:400]}"
                except Exception as _mtc_e:
                    print(f"[ORCH] mid_task_correct error (non-fatal): {_mtc_e}")

        kb_misses = [
            r for r in results_buffer
            if r["name"] in ("search_kb", "search_mistakes", "ask")
            and "KB_EMPTY" in _safe_result(r["result"], r["name"])
        ]
        if len(kb_misses) >= 2:
            current_user += (
                "\n\nKB_MISS_ALERT: KB search returned empty 2+ times. "
                "You MUST try direct table queries now:\n"
                f"- mistakes table: sb_query(table='mistakes', filters='id=gt.1', order='created_at.desc', limit='5', select='{_sel('mistakes')}')\n"
                f"- pattern_frequency: sb_query(table='pattern_frequency', filters='id=gt.1', order='frequency.desc', limit='8', select='{_sel('pattern_frequency')}')\n"
                f"- cold_reflections: sb_query(table='cold_reflections', filters='id=gt.1', order='created_at.desc', limit='5', select='{_sel('cold_reflections')}')\n"
                f"- knowledge_base: sb_query(table='knowledge_base', filters='id=gt.1&domain=like.*core*', limit='10', select='{_sel('knowledge_base')}')\n"
                f"- sessions: sb_query(table='sessions', filters='id=gt.1', order='created_at.desc', limit='3', select='{_sel('sessions')}')\n"
                f"- hot_reflections: sb_query(table='hot_reflections', filters='id=gt.1&processed_by_cold=eq.false', order='created_at.desc', limit='5', select='{_sel('hot_reflections')}')\n"
                "Do NOT call search_kb again with the same query. Use sb_query instead."
            )

        try:
            response = _call_model(
                system_prompt = system_prompt,
                history_text  = history_text,
                user_message  = current_user,
                tools_desc    = tools_desc,
                image_b64     = image_b64 if tool_call_count == 0 else None,
                image_mime    = image_mime,
                file_b64      = file_b64  if tool_call_count == 0 else None,
                file_mime     = file_mime,
                pre_flight    = pre_flight if tool_call_count == 0 else None,
            )
        except Exception as e:
            _tg_send(cid, f"❌ All providers failed: {str(e)[:300]}")
            return

        thought    = response.get("thought", "")
        tool_calls = response.get("tool_calls", [])
        reply      = response.get("reply", "")
        done       = response.get("done", False)

        if not tool_calls:
            effective_reply = reply.strip() if reply else ""

            if not effective_reply and results_buffer:
                try:
                    synth = _or_text(
                        system=(
                            f"{system_prompt}\n\n"
                            "The owner asked a question. You ran tools. "
                            "Now synthesize a clear, direct answer from the tool results."
                        ),
                        user=(
                            f"OWNER QUESTION: {user_message}\n\n"
                            f"TOOL RESULTS:\n" +
                            "\n".join(
                                f"[{r['name']}] → {r['result'][:300]}"
                                for r in results_buffer[-6:]
                            )
                        ),
                        max_tokens=800,
                    )
                    effective_reply = synth.strip()
                except Exception:
                    pass

            if effective_reply:
                check = _validate_before_reply(
                    user_message, effective_reply, results_buffer, system_prompt
                )
                if not check.get("is_valid", True) and check.get("corrected_reply"):
                    effective_reply = check["corrected_reply"].strip() or effective_reply
                    print(f"[ORCH] reply corrected by validator: {check.get('reason','')[:100]}")

                _tg_send(cid, effective_reply)
                _append_history(cid, "assistant", effective_reply)
                _record_reply(cid, user_message, effective_reply)  # P3-05
                with _metrics_lock: _metrics["loop_depths"].append(tool_call_count)
                threading.Thread(
                    target=_log_hot_reflection,
                    args=(user_message, results_buffer, effective_reply),
                    daemon=True,
                ).start()
            else:
                tools_called = [r["name"] for r in results_buffer] if results_buffer else []
                if tools_called:
                    lines = ["⚠️ No answer generated."]
                    lines.append(f"Tools called: {', '.join(tools_called)}")
                    for r in results_buffer[-3:]:
                        snippet = r["result"][:200].replace("<", "&lt;").replace(">", "&gt;")
                        lines.append(f"[{r['name']}] {snippet}")
                    _tg_send(cid, "\n".join(lines))
                else:
                    _tg_send(cid, "⚠️ No tools called and no answer — try rephrasing.")
            return

        # Execute tool calls
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args") or {}
            if not tool_name:
                continue

            if tool_name in ("list_tools", "get_tool_info") and not tool_args.get("search") and not tool_args.get("category") and not tool_args.get("name"):
                tool_args = {}
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
            if call_key in _seen_calls:
                results_buffer.append({
                    "name":   tool_name,
                    "result": '{"ok": true, "note": "already called this turn — result cached above"}',
                })
                continue
            _seen_calls.add(call_key)
            tool_call_count += 1

            if _is_destructive(tool_name, tool_args):
                confirmed = _request_confirmation(cid, tool_name, tool_args)
                if not confirmed:
                    results_buffer.append({
                        "name":   tool_name,
                        "result": '{"ok": false, "error": "CANCELLED by owner"}',
                    })
                    _tg_send(cid, "🚫 Action cancelled.")
                    continue

            # P1-04: Auto-wire loop_detect before every tool execution
            try:
                from core_tools import TOOLS as _AGI_TOOLS
                _ld_fn = _AGI_TOOLS.get("loop_detect", {}).get("fn")
                if _ld_fn:
                    _ld = _ld_fn(
                        action=tool_name,
                        context_hash=str(hash(json.dumps(tool_args, sort_keys=True, default=str)))[:8],
                        session_id=cid,
                    )
                    if _ld.get("loop_detected"):
                        print(f"[ORCH] P1-04 loop_detect: loop detected for {tool_name} — skipping")
                        results_buffer.append({
                            "name": tool_name,
                            "result": json.dumps({"ok": False, "error": f"LOOP_DETECTED: {tool_name} called {_ld.get('previous_attempts',0)+1}x this session with same args. Change approach."}),
                        })
                        continue
            except Exception as _ld_e:
                print(f"[ORCH] loop_detect error (non-fatal): {_ld_e}")

            is_desktop = tool_name.startswith("desktop_")
            if is_desktop:
                _tg_send(cid, "🖥 <i>Sending to PC...</i>")
                result_str = _execute_desktop_tool(tool_name, tool_args, cid)
            else:
                result_str = _execute_railway_tool(tool_name, tool_args, cid=cid)  # P1-06: pass cid

            # Auto-send screenshot
            try:
                rp = json.loads(result_str)
                if isinstance(rp, dict):
                    for k in ("base64", "screenshot", "image"):
                        v = rp.get(k)
                        if v and isinstance(v, str) and len(v) > 200:
                            _tg_photo(cid, v, caption=f"📸 {tool_name}")
                            break
                    for item in rp.get("results", []):
                        if isinstance(item, dict) and item.get("base64"):
                            _tg_photo(cid, item["base64"], caption="📸 Step screenshot")
                            break
            except Exception:
                pass

            compressed = _compress_result(result_str, tool_name)
            results_buffer.append({"name": tool_name, "result": compressed})
            with _metrics_lock:
                _metrics["tool_calls_total"] += 1
                if _safe_result(compressed, tool_name).startswith("TOOL_FAILED"):
                    _metrics["tool_calls_failed"] += 1

        # P1-04: Auto-wire circuit_breaker on cascade failure (3+ TOOL_FAILEDs)
        _cascade_fails = sum(
            1 for r in results_buffer
            if _safe_result(r["result"], r["name"]).startswith("TOOL_FAILED")
        )
        if _cascade_fails >= 3:
            try:
                from core_tools import TOOLS as _AGI_TOOLS
                _cb_fn = _AGI_TOOLS.get("circuit_breaker", {}).get("fn")
                if _cb_fn:
                    _failed_names = [r["name"] for r in results_buffer if _safe_result(r["result"], r["name"]).startswith("TOOL_FAILED")]
                    _cb = _cb_fn(
                        failed_step=_failed_names[-1] if _failed_names else "unknown",
                        dependent_steps=",".join(_failed_names[-3:]),
                        failure_reason=f"{_cascade_fails} consecutive tool failures",
                    )
                    if _cb.get("ok"):
                        current_user += f"\n\nCIRCUIT_BREAKER: {json.dumps(_cb, default=str)[:400]}"
            except Exception as _cb_e:
                print(f"[ORCH] circuit_breaker error (non-fatal): {_cb_e}")

        # Stall detection
        if tool_calls and tool_call_count == _prev_count:
            if reply and reply.strip():
                _tg_send(cid, reply.strip())
                _append_history(cid, "assistant", reply.strip())
                return
            elif results_buffer:
                try:
                    synth = _or_text(
                        system=(
                            f"{system_prompt}\n\n"
                            "Tools were called and results are available. "
                            "Synthesize a clear, direct answer from the tool results. "
                            "Label facts as CONFIRMED/INFERRED/UNKNOWN."
                        ),
                        user=(
                            f"OWNER QUESTION: {user_message}\n\n"
                            f"TOOL RESULTS:\n" +
                            "\n".join(
                                f"[{r['name']}] → {r['result'][:400]}"
                                for r in results_buffer[-6:]
                            )
                        ),
                        max_tokens=1000,
                    )
                    if synth.strip():
                        _tg_send(cid, synth.strip())
                        _append_history(cid, "assistant", synth.strip())
                        return
                except Exception:
                    pass
                stalled = ", ".join(tc.get("name", "?") for tc in tool_calls)
                last_r = results_buffer[-1]["result"][:300]
                _tg_send(cid, f"⚠️ Stalled — duplicate calls: {stalled}\nLast result: {last_r}")
            else:
                stalled = ", ".join(tc.get("name", "?") for tc in tool_calls)
                _tg_send(cid, f"⚠️ Stalled — no results: {stalled}")
            return

        # P3-03: suppress intermediate status in autonomous mode
        if tool_calls and tool_call_count % 3 == 1 and not is_autonomous:
            names = ", ".join(tc.get("name", "?") for tc in tool_calls)
            _tg_send(cid, f"⚙️ <i>{names}</i>")

        if done:
            if reply and reply.strip():
                final = reply.strip()
                _tg_send(cid, final)
                _append_history(cid, "assistant", final)
                _record_reply(cid, user_message, final)        # P3-05
                return

    last_tools = ", ".join(r["name"] for r in results_buffer[-3:]) if results_buffer else "none"
    last_result = results_buffer[-1]["result"][:200] if results_buffer else "none"
    msg = (
        f"⚠️ Hit tool call limit ({MAX_TOOL_CALLS}).\n"
        f"Last tools: {last_tools}\n"
        f"Last result: {last_result}\n"
        "Send a follow-up to continue."
    )
    _tg_send(cid, msg)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def handle_telegram_message(msg: dict):
    """Handle all free-text Telegram messages, photos, and document/file uploads."""
    cid    = str(msg.get("chat", {}).get("id", ""))
    text   = (msg.get("text") or msg.get("caption") or "").strip()
    photos = msg.get("photo")
    doc    = msg.get("document")

    if not cid:
        return
    if cid != str(TELEGRAM_CHAT):
        _tg_send(cid, "Unauthorized.")
        return

    if text and handle_confirm_reply(cid, text):
        return

    if text.startswith("/") and "@" in text:
        text = text.split("@")[0]

    lower = text.lower()

    # ── P3-05: detect implicit feedback on every incoming message ─────────────
    if text and not text.startswith("/"):
        _detect_implicit_feedback(cid, text)

    if lower in ("/clear", "clear", "reset", "forget"):
        _clear_history(cid)
        _tg_send(cid, "🧹 History and session cache cleared.")
        return

    if lower in ("/model", "which model"):
        _tg_send(
            cid,
            f"Model: <b>OpenRouter ({OPENROUTER_MODEL})</b>\n"
            f"Cheap calls: <b>OpenRouter ({OPENROUTER_FAST_MODEL})</b>\n"
            f"Fallback chain: OpenRouter → Gemini direct → Groq\n"
            f"Swap: change OPENROUTER_MODEL in core_orchestrator.py"
        )
        return

    if lower in ("/refresh", "refresh"):
        _invalidate_cache(cid)
        _tg_send(cid, "🔄 Session cache cleared — reloads on next message.")
        return

    if lower in ("/cancel", "cancel", "stop", "berhenti"):
        with _active_lock:
            entry = _active_loops.pop(cid, None)
        if entry:
            try:
                entry["lock"].release()
            except RuntimeError:
                pass
            elapsed = int(time.time() - entry.get("started_at", time.time()))
            _tg_send(cid, f"🛑 Loop cancelled (was running {elapsed}s). Ready for next message.")
        else:
            _tg_send(cid, "ℹ️ No active loop to cancel.")
        return

    if lower in ("/metrics", "metrics", "stats orch"):
        m = get_orchestrator_metrics()
        total = m.get("total_messages", 0)
        or_pct  = round(m["provider_or"]     / max(total,1) * 100)
        gem_pct = round(m["provider_gemini"] / max(total,1) * 100)
        grq_pct = round(m["provider_groq"]   / max(total,1) * 100)
        fail_r  = round(m["tool_calls_failed"] / max(m["tool_calls_total"],1) * 100, 1)
        lines = [
            "📊 <b>Orchestrator Metrics</b>",
            "",
            f"Messages: {total} | Direct: {m['direct_answers']} | Trivial fast-path: {m.get('trivial_fast_path',0)}",
            f"Cache hits: {m.get('cache_hits',0)}",
            f"Provider: OR {or_pct}% / Gemini {gem_pct}% / Groq {grq_pct}%",
            f"Tool calls: {m['tool_calls_total']} ({fail_r}% fail rate)",
            f"Avg loop depth: {m['avg_loop_depth']} | Max: {m['max_loop_depth']}" + (f" | P90: {m['loop_depth_p90']}" if m['loop_depth_p90'] else ""),
            "",
            f"P3-03 Autonomous runs: {m.get('autonomous_runs',0)}",
            f"P3-05 Feedback: +{m.get('implicit_positive',0)} / -{m.get('implicit_negative',0)}",
            f"P3-06 Predictive cache hits: {m.get('predictive_hits',0)}",
        ]
        _tg_send(cid, "\n".join(lines))
        return

    if lower in ("/p3status", "p3status"):
        with _predictive_cache_lock:
            pred = _predictive_cache.get(cid, {})
        pred_age = int(time.time() - pred.get("loaded_at", 0)) if pred else -1
        lines = [
            "🧠 <b>Phase 3 Status</b>",
            "",
            f"P3-03 Autonomous mode: <b>active</b> (triggers on multi-step intent)",
            f"P3-05 Implicit feedback: <b>active</b> (3min window, short messages)",
            f"P3-06 Predictive cache: pattern=<b>{pred.get('pattern_key','none')}</b> age={pred_age}s",
            f"P3-01 Semantic KB: <b>ready</b> (activate after adding vector column to Supabase)",
            f"P3-04 Episode memory: <b>ready</b> (activate after adding conversation_episodes table)",
        ]
        _tg_send(cid, "\n".join(lines))
        return

    # ── Attachment handling ────────────────────────────────────────────────────
    image_b64  = None
    image_mime = "image/jpeg"
    file_b64   = None
    file_mime  = None

    if photos:
        best    = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = best.get("file_id")
        if file_id:
            _tg_send(cid, "📷 Downloading image...")
            image_b64 = _tg_download_file(file_id)
            if not image_b64:
                _tg_send(cid, "❌ Failed to download image.")
                return
        if not text:
            text = "Describe and analyse this image."

    elif doc:
        file_id   = doc.get("file_id")
        file_mime = doc.get("mime_type") or "application/octet-stream"
        file_name = doc.get("file_name") or "attachment"
        file_size = doc.get("file_size") or 0

        if file_size > 20 * 1024 * 1024:
            _tg_send(cid, "❌ File too large (max 20 MB).")
            return

        _tg_send(cid, f"📎 Downloading <b>{file_name}</b> ({file_size // 1024} KB)...")
        raw_b64 = _tg_download_file(file_id)
        if not raw_b64:
            _tg_send(cid, "❌ Failed to download file.")
            return

        if file_mime.startswith("image/"):
            image_b64  = raw_b64
            image_mime = file_mime
        else:
            file_b64 = raw_b64

        if not text:
            ext  = _TG_MIME_EXT.get(file_mime, "file")
            text = f"I've sent you a {ext.upper()} file named '{file_name}'. Please read and analyse it."

    if not text and not image_b64 and not file_b64:
        return

    print(f"[ORCH] [{cid}] {text[:80]}"
          + (" [img]" if image_b64 else "")
          + (f" [file:{file_mime}]" if file_b64 else ""))
    _append_history(cid, "user", text, image_b64=image_b64, image_mime=image_mime)

    # Per-cid concurrency gate with hard timeout
    with _active_lock:
        entry = _active_loops.get(cid)
        if entry and (time.time() - entry["started_at"]) > LOOP_HARD_TIMEOUT:
            print(f"[ORCH] Force-releasing stale lock for {cid} (>{LOOP_HARD_TIMEOUT}s)")
            try:
                entry["lock"].release()
            except RuntimeError:
                pass
            del _active_loops[cid]
            entry = None
        if cid not in _active_loops:
            lock = threading.Lock()
            _active_loops[cid] = {"lock": lock, "started_at": time.time(), "message": text[:80]}
        else:
            lock = None

    if lock is None:
        entry = _active_loops.get(cid, {})
        elapsed = int(time.time() - entry.get("started_at", time.time()))
        prev_msg = entry.get("message", "?")
        wait_msg = f"⏳ Still processing (<b>{elapsed}s</b>): <i>{prev_msg}</i>\nSend /cancel to force-stop, or wait (auto-timeout {LOOP_HARD_TIMEOUT}s)."
        _tg_send(cid, wait_msg)
        return

    cid_lock = _active_loops[cid]["lock"]
    cid_lock.acquire()
    try:
        _agentic_loop(
            cid, text,
            image_b64=image_b64, image_mime=image_mime,
            file_b64=file_b64,   file_mime=file_mime,
        )
    except Exception as e:
        _tg_send(cid, f"❌ Error: {str(e)[:300]}")
        print(f"[ORCH] agentic_loop error:\n{traceback.format_exc()}")
    finally:
        cid_lock.release()
        with _active_lock:
            _active_loops.pop(cid, None)


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-MIGRATION — create telegram_conversations if missing
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_table():
    DDL = """
CREATE TABLE IF NOT EXISTS telegram_conversations (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT,
    deleted    BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tgconv_chat    ON telegram_conversations(chat_id);
CREATE INDEX IF NOT EXISTS idx_tgconv_created ON telegram_conversations(created_at DESC);
"""
    try:
        pat = SUPABASE_PAT
        if not pat:
            rows = sb_get(
                "knowledge_base",
                f"select={_sel_force('knowledge_base', ['content'])}&domain=eq.system.config&topic=eq.supabase_pat&limit=1",
                svc=True,
            )
            pat = (rows[0].get("content", "") if rows else "").strip()
        if not pat:
            print("[ORCH] _ensure_table: no PAT available — skipping")
            return
        resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
            headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"},
            json={"query": DDL},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            print("[ORCH] telegram_conversations table ensured.")
        else:
            print(f"[ORCH] _ensure_table: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[ORCH] _ensure_table error (non-fatal): {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC DESKTOP RESULT POLLER
# ══════════════════════════════════════════════════════════════════════════════

def _desktop_result_poller():
    from collections import deque as _deque
    notified_dq: _deque = _deque(maxlen=500)
    notified: set = set()
    print("[ORCH] Desktop result poller started")
    while True:
        try:
            rows = sb_get(
                "task_queue",
                f"select={_sel_force('task_queue', ['id','status','result','error','chat_id','updated_at','created_at'])}"
                "&source=eq.mcp_session"
                "&status=in.(done,failed)"
                "&order=created_at.desc&limit=20",
            ) or []
            for row in rows:
                tid = str(row.get("id", ""))
                if tid in notified:
                    continue
                cid = row.get("chat_id")
                if not cid:
                    notified.add(tid)
                    continue
                status = row.get("status")
                result = row.get("result") or row.get("error") or "no result"
                icon   = "✅" if status == "done" else "❌"
                _tg_send(
                    cid,
                    f"{icon} Async task <code>{tid[:8]}</code> {status}:\n"
                    f"<code>{result[:500]}</code>"
                )
                notified.add(tid)
                notified_dq.append(tid)
                if len(notified_dq) == 500:
                    notified = set(notified_dq)
        except Exception as e:
            print(f"[ORCH] poller error: {e}")
        time.sleep(15)


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def start_orchestrator():
    """Start all orchestrator background threads."""
    threading.Thread(target=_ensure_table,          daemon=True, name="orch_ensure_table").start()
    threading.Thread(target=_desktop_result_poller, daemon=True, name="orch_result_poller").start()
    threading.Thread(target=_run_predictive_loader, daemon=True, name="orch_predictive_loader").start()  # P3-06
    print(f"[ORCH] Started. Provider: OpenRouter ({OPENROUTER_MODEL}) | "
          f"Fast: {OPENROUTER_FAST_MODEL} | "
          f"Fallback: Gemini ({GEMINI_FALLBACK_MODEL}) → Groq ({GROQ_LAST_RESORT_MODEL}) | "
          f"P1-03 trivial fast-path: ON | P1-06 READ cache TTL: {_TOOL_CACHE_TTL}s | "
          f"P3-03 autonomous: ON | P3-05 implicit feedback: ON | P3-06 predictive loader: ON")
