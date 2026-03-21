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
  OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"   # current default
  OPENROUTER_MODEL = "anthropic/claude-sonnet-4-5"     # if want Claude
  OPENROUTER_MODEL = "openai/gpt-4o"                   # if want GPT

VARIABLES VERIFIED AGAINST ACTUAL CODEBASE:
  core_config.py  : TELEGRAM_CHAT, SUPABASE_PAT, SUPABASE_REF,
                    sb_get(t, qs, svc), sb_post(t, d), sb_patch(t, m, d),
                    gemini_chat(system, user, max_tokens, json_mode),
                    groq_chat(system, user, model, max_tokens)
  core_tools.py   : TOOLS dict keys are fn/perm/args/desc
  core_main.py    : handle_msg(msg) uses cid/text, on_start() @app.on_event
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
OPENROUTER_MODEL       = "anthropic/claude-haiku-4-5"   # swap here to change model
OPENROUTER_FAST_MODEL  = "google/gemini-2.5-flash-lite"   # cheap calls: tool select, compress
                                                           # swap to "meta-llama/llama-3.3-70b-instruct:free"
                                                           # or any fast OR model if needed

MODEL_PROVIDER = "openrouter"  # display label only

# Gemini fallback model (direct API)
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Groq last resort
GROQ_LAST_RESORT_MODEL = "llama-3.3-70b-versatile"

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
}
_metrics_lock = threading.Lock()

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

_ALWAYS_TOOLS = {
    "search_kb", "get_mistakes", "list_tools", "get_tool_info", "get_behavioral_rules", "get_table_schema",
}

_TOOL_CATEGORIES = {
    "deploy":    ["redeploy", "build_status", "deploy_and_wait", "validate_syntax",
                  "patch_file", "multi_patch", "gh_search_replace", "railway_logs_live"],
    "code":      ["read_file", "write_file", "gh_read_lines", "search_in_file",
                  "core_py_fn", "core_py_validate", "append_to_file", "diff"],
    "training":  ["trigger_cold_processor", "get_training_pipeline", "list_evolutions",
                  "approve_evolution", "reject_evolution", "check_evolutions",
                  "bulk_reject_evolutions", "backfill_patterns"],
    "system":    ["get_state", "get_system_health", "stats", "build_status",
                  "crash_report", "system_map_scan", "sync_system_map"],
    "railway":   ["railway_env_get", "railway_env_set", "railway_logs_live",
                  "railway_service_info", "redeploy", "build_status"],
    "knowledge": ["search_kb", "add_knowledge", "kb_update", "get_mistakes",
                  "search_mistakes", "ask"],
    "task":      ["task_add", "task_update", "task_health", "synthesize_evolutions",
                  "sb_query", "sb_insert", "sb_patch"],
    "crypto":    ["crypto_price", "crypto_balance", "crypto_trade"],
    "project":   ["project_list", "project_get", "project_search", "project_register",
                  "project_update_kb", "project_index"],
    "agentic":   ["reason_chain", "lookahead", "decompose_task", "negative_space",
                  "predict_failure", "action_gate", "loop_detect"],
    "web":       ["web_search", "web_fetch", "summarize_url"],
    "document":  ["create_document", "create_spreadsheet", "create_presentation",
                  "read_document", "convert_document"],
    "image":     ["generate_image", "image_process"],
    "utils":     ["weather", "calc", "datetime_now", "currency", "translate", "run_python",
                  "list_tools", "get_tool_info", "get_table_schema"]
}


def _select_tools(message: str, history_summary: str) -> list:
    """
    Use OpenRouter fast model to pick relevant tool categories.
    Fallback chain: OpenRouter → Gemini → Groq (handled by _or_text).
    Dynamically adds 'misc' category for any live tools not in static categories.
    """
    try:
        from core_tools import TOOLS
        all_tool_names = set(TOOLS.keys())
        # Populate misc with tools not in any static category (new tools from evolutions)
        categorised = {t for tools in _TOOL_CATEGORIES.values() for t in tools}
        misc_tools = [t for t in all_tool_names if t not in categorised]
        # Use local merged dict — never mutate global _TOOL_CATEGORIES (not thread-safe)
        tool_cats = dict(_TOOL_CATEGORIES)
        if misc_tools:
            tool_cats["misc"] = misc_tools
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
            return []


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CONTEXT — cached, loaded once per SESSION_CACHE_TTL
# ══════════════════════════════════════════════════════════════════════════════


def _build_dynamic_context(recent_text: str = "") -> str:
    """
    Query Supabase for KB content relevant to the current conversation.
    Returns a formatted string injected into the system prompt as PRECEDENTS.

    Queries 3 sources in parallel (threads):
      1. knowledge_base — entries matching recent message keywords
      2. pattern_frequency — top recurring patterns with solutions
      3. cold_reflections — most recent distilled learnings

    Result is cached per session alongside system_prompt.
    Called once per message if session cache is cold.
    """
    parts = []
    recent_lower = recent_text.lower()[:200]

    # Extract keywords from recent conversation for KB search
    # Strip common words, keep nouns/verbs likely to match KB topics
    import re as _re
    stop = {"the","a","an","is","are","was","were","i","you","we","it","to","of",
            "and","or","in","on","at","for","with","do","did","can","could","would",
            "please","kalau","yang","ada","ini","itu","ke","dari","sudah","bisa",
            "mau","perlu","tidak","bukan","tapi","juga","saya","kamu","dia"}
    words = [w for w in _re.findall(r"[a-zA-Z]{4,}", recent_lower) if w not in stop]
    keywords = list(dict.fromkeys(words))[:5]  # deduplicated, max 5

    results = {}

    def _fetch_kb():
        try:
            if not keywords:
                return
            # Search KB for entries matching any keyword
            # Use OR filter: topic contains keyword1 OR keyword2...
            filters = ",".join(f"topic.ilike.*{k}*,content.ilike.*{k}*" for k in keywords[:3])
            rows = sb_get(
                "knowledge_base",
                f"select={_sel_force('knowledge_base', ['domain','topic','content','source_type'])}"
                f"&or=({filters})"
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

    def _fetch_patterns():
        try:
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

    # Run all 3 fetches concurrently
    import threading as _threading
    threads = [
        _threading.Thread(target=_fetch_kb,          daemon=True),
        _threading.Thread(target=_fetch_patterns,    daemon=True),
        _threading.Thread(target=_fetch_reflections, daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=4)  # max 4s total — never block prompt build

    if not results:
        return ""

    sections = []
    if results.get("kb"):          sections.append(results["kb"])
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
            qa         = ss.get("quality_alert")
            live_tools = ss.get("live_tool_count", 0)

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

            # Dynamic KB injection — live context relevant to this conversation
            dynamic_ctx = _build_dynamic_context(recent_text)
            if dynamic_ctx:
                parts.append(dynamic_ctx)

            rules = ss.get("behavioral_rules", []) or []
            if rules:
                r_lines = "\n".join(
                    f"  [{r.get('trigger','')}] {r.get('pointer','')[:100]}"
                    for r in rules[:15]
                )
                parts.append(f"BEHAVIORAL RULES:\n{r_lines}")
    except Exception as e:
        print(f"[ORCH] session_start error (non-fatal): {e}")
        # Still inject dynamic context even if session_start failed
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

    # Truncate at part boundary to avoid cutting mid-principle
    full = "\n\n".join(parts)
    if len(full) > MAX_CONTEXT_CHARS:
        # Drop trailing parts until fits — always keep identity + constitution
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
            "content":    content[:1500],  # match _append_history truncation
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
            _compress_history(q)
        q.append(entry)
    _sb_save_msg(cid, role, content)


def _compress_history(q: deque):
    """Compress oldest N turns into summary. Uses OpenRouter (fallback chain inside _or_text)."""
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
    try:
        sb_patch("telegram_conversations", f"chat_id=eq.{cid}", {"deleted": True})
    except Exception:
        pass
    _invalidate_cache(cid)


def _history_to_text(history: list) -> str:
    lines = []
    for h in history[-MAX_HISTORY_TURNS:]:
        role = h.get("role", "user")
        # Assistant responses carry more context value — give them more chars
        limit = 600 if role == "assistant" else 200
        content = h.get("content", "")[:limit]
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# METACOGNITIVE LAYER
# Two lightweight model calls that wrap the agentic loop.
# ══════════════════════════════════════════════════════════════════════════════

def _reason_before_execute(user_message: str, system_prompt: str,
                            history_text: str, tools_desc: str) -> dict:
    """
    Pre-execution reasoning pass with active 4-brain query.
    Queries KB + mistakes + behavioral_rules + list_tools before reasoning.
    """
    # ── Active brain query sebelum reasoning ──────────────────────────────────
    brain_context = ""
    try:
        from core_tools import TOOLS

        # Brain 1: KB — t_search_kb returns list directly
        kb_fn = TOOLS.get("search_kb", {}).get("fn")
        if kb_fn:
            kb = kb_fn(query=user_message[:100], limit="5")  # no domain filter — search all
            kb_list = kb if isinstance(kb, list) else kb.get("results", []) if isinstance(kb, dict) else []
            if kb_list:
                brain_context += "KB CONTEXT:\n" + "\n".join(
                    f"  [{r.get('topic','')}] {r.get('content','')[:150]}"
                    for r in kb_list[:5]
                ) + "\n"

        # Brain 2: Mistakes — t_get_mistakes returns list directly
        m_fn = TOOLS.get("get_mistakes", {}).get("fn")
        if m_fn:
            m = m_fn(limit="3")  # no domain filter — search all
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

        # Brain 4: Tool discovery — find tools relevant to this message
        lt_fn = TOOLS.get("list_tools", {}).get("fn")
        if lt_fn:
            lt = lt_fn(search=user_message[:50])
            if lt.get("ok") and lt.get("tools"):
                brain_context += "RELEVANT TOOLS:\n" + "\n".join(
                    f"  {t['name']}({t['args']}) — {t['desc'][:80]}"
                    for t in lt["tools"][:8]
                ) + "\n"

        # Brain 5: Meta/self-knowledge — patterns + reflections
        # Always query for meta questions (failure modes, patterns, self-awareness)
        meta_keywords = ["pattern", "failure", "mistake", "learn", "improve",
                         "session", "history", "trend", "reflect", "stale",
                         "outdated", "know", "aware", "self"]
        is_meta = any(kw in user_message.lower() for kw in meta_keywords)
        if is_meta:
            # Query pattern_frequency directly
            pf_result = sb_get(
                "pattern_frequency",
                f"select={_sel('pattern_frequency')}&id=gt.1&order=frequency.desc&limit=8",
            )
            if pf_result:
                brain_context += "TOP PATTERNS (from training):\n" + "\n".join(
                    "  [" + r.get("domain","?") + "/" + str(r.get("frequency",0)) + "x] " + (r.get("pattern_key") or r.get("pattern",""))[:120]
                    for r in pf_result[:8]
                ) + "\n"
            # Query cold_reflections for distilled learnings
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
            # Query mistakes with direct sb_get (bypasses search_kb issues)
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

        # Brain 6: Accountability — sessions + hot_reflections for "why/deviation/past" questions
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
    """
    Post-execution self-check (OpenRouter fast model).
    Returns {
        "is_valid": bool,           # does reply actually answer the question?
        "corrected_reply": str,     # improved reply if not valid (may use results_buffer)
        "reason": str,              # why valid/invalid
    }
    Falls back to {"is_valid": True} on error — always delivers something.
    """
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
# Chain: OpenRouter → Gemini direct → Groq
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

    # Inject creative_path hint from pre-flight if available
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
        # Route by type: images as image_url, PDFs as document, others as text hint
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
    """
    Main reasoning call. Chain: OpenRouter → Gemini → Groq.
    Returns {"thought": str, "tool_calls": [...], "reply": str, "done": bool}
    """
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
            # Response was truncated — attempt partial parse, else treat as plain reply
            print(f"[ORCH] WARNING: model output truncated (finish_reason=length)")
            raw_text = raw_text.rstrip().rstrip(",") + "}"  # best-effort JSON close
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

    # 2. Gemini direct (text + image; no arbitrary file)
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

    # 3. Groq last resort (text only)
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
    TOTAL_DESC_BUDGET = 12000  # cap total tool desc to avoid blowing context window
    try:
        from core_tools import TOOLS
        for name in selected_tool_names:
            if total_chars >= TOTAL_DESC_BUDGET:
                lines.append(f"  ... ({len(selected_tool_names) - len(lines)} more tools omitted — use list_tools to discover)")
                break
            tdef = TOOLS.get(name)
            if not tdef:
                continue
            args_str = ", ".join(
                (a["name"] if isinstance(a, dict) else a)
                for a in (tdef.get("args") or [])
            )
            desc = tdef.get("desc", "")[:300]  # 300 chars per tool, not 4096
            line = f"  {name}({args_str}) — {desc}"
            lines.append(line)
            total_chars += len(line)
    except Exception:
        pass
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
    # Meta-tools: never truncate — model needs full data
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


def _execute_railway_tool(tool_name: str, tool_args: dict) -> str:
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
        return compressed
    except TypeError as e:
        # Wrong args — give model exact hint to fix
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
    # Check tool_name directly first (exact match on dangerous tool names)
    tn = tool_name.lower()
    if any(tn == kw or tn.startswith(kw + "_") or tn.endswith("_" + kw)
           for kw in _DESTRUCTIVE_KW):
        return True
    # Check args with word boundary to avoid false positives like "deleted", "node_deleted"
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
        # Split long messages into chunks
        chunk_size = 4000  # sedikit di bawah 4096 untuk safety
        if len(text) <= chunk_size:
            notify(text, cid=cid)
        else:
            chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            for chunk in chunks:
                notify(chunk, cid=cid)
                time.sleep(0.3)  # avoid Telegram flood limit
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
            # Failure signal
            if parsed.get("ok") is False or parsed.get("error"):
                return f"TOOL_FAILED: {json.dumps(parsed, default=str)[:300]}"
            # KB empty signal
            if tool_name in ("search_kb", "search_mistakes", "ask"):
                results = parsed.get("results") or parsed.get("items") or []
                if not results:
                    return (
                        "KB_EMPTY: no results. Try: different keywords, "
                        "sb_query direct tables, web_search, or synthesize from context."
                    )
    except Exception:
        pass
    # Everything else — pass through raw, let model read it directly
    return r


def _log_hot_reflection(user_message: str, results_buffer: list, reply: str):
    """Log hot_reflection after every completed agentic loop.
    Tries TOOLS first, falls back to direct sb_post. Never blocks.
    Runs in background thread — reply already delivered before this runs."""
    tools_used = [r["name"] for r in results_buffer]
    tool_count = len(tools_used)
    failed     = sum(1 for r in results_buffer if _safe_result(r["result"], r["name"]).startswith("TOOL_FAILED"))
    quality    = max(0.3, 0.9 - (failed * 0.1))  # degrade quality for each failure
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
        # Try known tool names first
        for name in ("add_hot_reflection", "log_hot_reflection", "write_hot_reflection"):
            fn = _T.get(name, {}).get("fn")
            if fn:
                fn(**{k: v for k, v in payload.items()
                      if k in ("domain", "task_summary", "quality_score", "reflection_text", "source")})
                return
    except Exception:
        pass
    # Fallback: direct sb_post — guaranteed path
    try:
        sb_post("hot_reflections", payload)
    except Exception as e:
        print(f"[ORCH] _log_hot_reflection failed: {e}")


def _agentic_loop(cid: str, user_message: str,
                  image_b64: str = None, image_mime: str = "image/jpeg",
                  file_b64: str = None, file_mime: str = None):
    """
    Full agentic loop with metacognitive wrapper:
    0. REASON — pre-flight: intent + plan (can short-circuit if answerable directly)
    1. SELECT tools
    2. EXECUTE loop — tool calls, result injection, failure escalation
    3. VALIDATE — self-check reply before sending to owner
    """
    with _metrics_lock: _metrics["total_messages"] += 1

    history       = _get_history(cid)
    history_text  = _history_to_text(history)
    system_prompt = _build_system_prompt(cid, recent_text=user_message)

    # ── Phase 0: Reason before executing ──────────────────────────────────────
    selected_tools = _select_tools(user_message, history_text)
    tools_desc     = _build_tools_desc(selected_tools)

    pre_flight = _reason_before_execute(user_message, system_prompt, history_text, tools_desc)

    # Can answer directly without tools?
    if pre_flight.get("can_answer_directly") and pre_flight.get("direct_answer"):
        direct = pre_flight["direct_answer"].strip()
        if direct:
            # Still validate before sending
            check = _validate_before_reply(user_message, direct, [], system_prompt)
            final = (check.get("corrected_reply") or direct) if not check.get("is_valid", True) else direct
            _tg_send(cid, final)
            _append_history(cid, "assistant", final)
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
            # Sleep based on last executed tool type to give infra time to settle
            last_tool = results_buffer[-1]["name"] if results_buffer else ""
            if last_tool in ("redeploy", "deploy_and_wait"):
                _sleep = 15  # deploy needs more time to propagate
            elif last_tool in ("patch_file", "multi_patch", "gh_search_replace", "write_file"):
                _sleep = 5   # file writes: give Railway time before next read
            elif last_tool.startswith("desktop_"):
                _sleep = 3   # desktop tasks have internal latency
            else:
                _sleep = 1
            time.sleep(_sleep)
        _tg_typing(cid)

        # Build user content for this loop iteration
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

        # Failure escalation
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

        # KB miss escalation — after 2+ empty KB searches, inject strategy
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

        # No tool calls — model wants to reply or is stuck
        if not tool_calls:
            # ── Phase 3: Validate before reply ───────────────────────────────
            effective_reply = reply.strip() if reply else ""

            if not effective_reply and results_buffer:
                # Model has results but gave no reply — force synthesis
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
                with _metrics_lock: _metrics["loop_depths"].append(tool_call_count)
                # Session close: log hot_reflection for learning capture
                threading.Thread(
                    target=_log_hot_reflection,
                    args=(user_message, results_buffer, effective_reply),
                    daemon=True,
                ).start()
            else:
                # No reply and no results — show debug summary
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

            # Normalize meta-tools with no meaningful args to prevent false duplicates
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

            is_desktop = tool_name.startswith("desktop_")
            if is_desktop:
                _tg_send(cid, "🖥 <i>Sending to PC...</i>")
                result_str = _execute_desktop_tool(tool_name, tool_args, cid)
            else:
                result_str = _execute_railway_tool(tool_name, tool_args)

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

        if tool_calls and tool_call_count % 3 == 1:  # send status every 3 iterations, not every one
            names = ", ".join(tc.get("name", "?") for tc in tool_calls)
            _tg_send(cid, f"⚙️ <i>{names}</i>")

        if done:
            # done=True with tool_calls means tools ran this round but model signals finish
            # Next loop iteration will have empty tool_calls and handle the reply properly
            # If reply is already set here, send it directly (avoid double loop)
            if reply and reply.strip():
                final = reply.strip()
                _tg_send(cid, final)
                _append_history(cid, "assistant", final)
                return
            # No reply yet — let loop continue once more to synthesize

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
    """
    Handle all free-text Telegram messages, photos, and document/file uploads.
    Must be called in a background thread from core_main.py handle_msg().
    """
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
            f"Messages: {total} | Direct answers: {m['direct_answers']}",
            f"Provider: OR {or_pct}% / Gemini {gem_pct}% / Groq {grq_pct}%",
            f"Tool calls: {m['tool_calls_total']} ({fail_r}% fail rate)",
            f"Avg loop depth: {m['avg_loop_depth']} | Max: {m['max_loop_depth']}" + (f" | P90: {m['loop_depth_p90']}" if m['loop_depth_p90'] else ""),
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
        # Auto-release stale lock if past hard timeout
        if entry and (time.time() - entry["started_at"]) > LOOP_HARD_TIMEOUT:
            print(f"[ORCH] Force-releasing stale lock for {cid} (>{LOOP_HARD_TIMEOUT}s)")
            try:
                entry["lock"].release()
            except RuntimeError:
                pass  # already released
            del _active_loops[cid]
            entry = None
        if cid not in _active_loops:
            lock = threading.Lock()
            _active_loops[cid] = {"lock": lock, "started_at": time.time(), "message": text[:80]}
        else:
            lock = None

    if lock is None:
        # Still busy — tell owner how long it has been running
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
    notified_dq: _deque = _deque(maxlen=500)  # sliding window — oldest auto-evicted, no re-notify risk
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
                # Sync set with deque window — remove oldest evicted entries
                if len(notified_dq) == 500:
                    notified = set(notified_dq)
        except Exception as e:
            print(f"[ORCH] poller error: {e}")
        time.sleep(15)


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def start_orchestrator():
    """
    Start all orchestrator background threads.
    Call from core_main.py on_start() after existing thread starts.
    """
    threading.Thread(target=_ensure_table,          daemon=True, name="orch_ensure_table").start()
    threading.Thread(target=_desktop_result_poller, daemon=True, name="orch_result_poller").start()
    print(f"[ORCH] Started. Provider: OpenRouter ({OPENROUTER_MODEL}) | "
          f"Fast: {OPENROUTER_FAST_MODEL} | "
          f"Fallback: Gemini ({GEMINI_FALLBACK_MODEL}) → Groq ({GROQ_LAST_RESORT_MODEL})")
