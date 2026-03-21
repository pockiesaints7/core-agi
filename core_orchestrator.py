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


# ══════════════════════════════════════════════════════════════════════════════
# MODEL CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Primary model for ALL calls — reasoning, tool selection, compression, metacognition
OPENROUTER_MODEL       = "google/gemini-2.5-flash-lite"   # swap here to change model
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
MAX_TOOL_RESULT_CHARS = 800
MAX_CONTEXT_CHARS     = 10000
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


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _strip_json(s: str) -> str:
    """Strip markdown code fences from model JSON output."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?", "", s)
        s = re.sub(r"```$", "", s)
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
        raise RuntimeError("OpenRouter 429 rate limited")
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
        return data["choices"][0]["message"]["content"] or ""
    except Exception as e:
        print(f"[ORCH] _or_text OpenRouter failed: {e}")

    # 2. Gemini direct
    try:
        return gemini_chat(
            system=system, user=user,
            max_tokens=max_tokens, json_mode=json_mode,
        )
    except Exception as e:
        print(f"[ORCH] _or_text Gemini failed: {e}")

    # 3. Groq last resort
    try:
        from core_config import groq_chat
        return groq_chat(
            system=system, user=user,
            model=GROQ_LAST_RESORT_MODEL, max_tokens=max_tokens,
        )
    except Exception as e:
        raise RuntimeError(f"All providers failed in _or_text: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN-OPTIMISED TOOL SELECTION — via OpenRouter
# ══════════════════════════════════════════════════════════════════════════════

_ALWAYS_TOOLS = {"search_kb", "get_mistakes", "task_update", "sb_query"}

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
    "utils":     ["weather", "calc", "datetime_now", "currency", "translate", "run_python"],
}


def _select_tools(message: str, history_summary: str) -> list:
    """
    Use OpenRouter fast model to pick relevant tool categories.
    Fallback chain: OpenRouter → Gemini → Groq (handled by _or_text).
    """
    try:
        from core_tools import TOOLS
        all_tool_names = set(TOOLS.keys())
        categories_text = ", ".join(_TOOL_CATEGORIES.keys())
        raw = _or_text(
            system=(
                "You are a tool router. Given a user message, output ONLY a JSON array "
                f"of category names from: [{categories_text}]. "
                "Output only valid JSON array of strings, no preamble."
            ),
            user=f"Message: {message[:300]}\nHistory: {history_summary[:200]}",
            max_tokens=80,
            json_mode=False,
        )
        selected_cats = json.loads(_strip_json(raw))
        if not isinstance(selected_cats, list):
            raise ValueError("not a list")
        selected_tools = set(_ALWAYS_TOOLS)
        for cat in selected_cats:
            selected_tools.update(_TOOL_CATEGORIES.get(cat, []))
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

def _build_system_prompt(cid: str) -> str:
    with _cache_lock:
        cached = _session_cache.get(cid)
        if cached and (time.time() - cached["loaded_at"]) < SESSION_CACHE_TTL:
            return cached["system_prompt"]

    parts = [
        "You are CORE, a personal AGI orchestration system owned by REINVAGNAR "
        "(Jakarta, Indonesia, UTC+7). Operating via Telegram. "
        "Full autonomous access to Railway tools AND the owner's PC. "
        "Be direct, agentic, thorough. Execute without asking unless action is destructive. "
        "Never assume — query Supabase or the PC first. Think step by step."
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
                    p.get("pattern", "")[:80] for p in patterns[:3]
                )
                parts.append(f"TOP PATTERNS: {p_lines}")
            if qa:
                parts.append(f"QUALITY ALERT: {qa}")
            rules = ss.get("behavioral_rules", []) or []
            if rules:
                r_lines = "\n".join(
                    f"  [{r.get('trigger','')}] {r.get('pointer','')[:100]}"
                    for r in rules[:15]
                )
                parts.append(f"BEHAVIORAL RULES:\n{r_lines}")
    except Exception as e:
        print(f"[ORCH] session_start error (non-fatal): {e}")

    parts.append(
        "RAILWAY TOOLS (no prefix — run on server instantly):\n"
        "  web_search(query, max_results) — search web\n"
        "  web_fetch(url, max_chars) — fetch URL content\n"
        "  summarize_url(url, focus) — fetch + summary\n"
        "  create_document(content, filename, format) — docx|pdf|txt|md|csv\n"
        "  create_spreadsheet(data, filename, format) — xlsx|csv\n"
        "  create_presentation(slides, filename, theme) — pptx\n"
        "  read_document(base64_content, format) — extract text\n"
        "  convert_document(base64_content, from_format, to_format)\n"
        "  generate_image(prompt, aspect_ratio) — Gemini Imagen\n"
        "  image_process(base64_content, operation, params)\n"
        "  weather(location) — default Jakarta\n"
        "  calc(expression) — safe math\n"
        "  datetime_now(timezone) — default Asia/Jakarta\n"
        "  currency(amount, from_cur, to_cur) — live rate\n"
        "  translate(text, target_language)\n"
        "  run_python(code, timeout) — execute Python on Railway"
    )
    parts.append(
        "DESKTOP TOOLS (prefix desktop_ — requires PC online):\n"
        "  desktop_run_script:  {script, lang: powershell|python}\n"
        "  desktop_file_ops:    {path, operation: read|write|list|delete|exists|move|mkdir|info|append, content?}\n"
        "  desktop_browser:     {url?, steps: [{action, selector?, value?, script?}], screenshot?}\n"
        "  desktop_search_web:  {query, max_results?}\n"
        "  desktop_cmd:         {command?, script?}"
    )
    parts.append(
        "CONSTITUTION: Owner=REINVAGNAR always. "
        "Never expose credentials. "
        "Never take destructive action without owner approval. "
        "When in doubt, do less and ask."
    )

    prompt = "\n\n".join(parts)[:MAX_CONTEXT_CHARS]
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
            "content":    content[:2000],
            "created_at": datetime.utcnow().isoformat(),
        })
    except Exception:
        pass


def _sb_load_history(cid: str) -> list:
    try:
        rows = sb_get(
            "telegram_conversations",
            f"select=role,content,created_at"
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
    for h in history[-12:]:
        role    = h.get("role", "user").upper()
        content = h.get("content", "")[:400]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# METACOGNITIVE LAYER
# Two lightweight model calls that wrap the agentic loop.
# ══════════════════════════════════════════════════════════════════════════════

def _reason_before_execute(user_message: str, system_prompt: str,
                            history_text: str, tools_desc: str) -> dict:
    """
    Pre-execution reasoning pass (OpenRouter fast model).
    Returns {
        "can_answer_directly": bool,   # true if no tools needed
        "direct_answer": str,          # if can_answer_directly
        "intent": str,                 # true intent parsed from message
        "plan": [str],                 # ordered steps
        "known_context": str,          # what we already know relevant to this
    }
    Falls back to empty plan on any error — loop continues normally.
    """
    prompt = (
        f"{system_prompt}\n\n"
        f"CONVERSATION:\n{history_text}\n\n"
        "Before executing any tools, reason about the owner's request.\n"
        "Output ONLY valid JSON:\n"
        "{\n"
        '  "intent": "true intent behind the message",\n'
        '  "known_context": "what you already know from session_start/history relevant to this",\n'
        '  "can_answer_directly": true/false,\n'
        '  "direct_answer": "full answer if can_answer_directly=true, else empty string",\n'
        '  "plan": ["step 1", "step 2", ...],\n'
        '  "fallback_strategy": "if primary tools return empty, what to try next"\n'
        "}"
    )
    try:
        raw = _or_text(
            system=prompt,
            user=f"OWNER MESSAGE: {user_message}",
            max_tokens=400,
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
        f"[{r['name']}] → {r['result'][:200]}" for r in results_buffer[-6:]
    ) or "No tools called."

    prompt = (
        "You are a quality checker for CORE AGI. "
        "Given an owner question and a draft reply, decide if the reply actually answers the question.\n\n"
        f"OWNER QUESTION: {user_message}\n\n"
        f"TOOL RESULTS AVAILABLE:\n{results_summary}\n\n"
        f"DRAFT REPLY: {reply or '(empty)'}\n\n"
        "Output ONLY valid JSON:\n"
        "{\n"
        '  "is_valid": true/false,\n'
        '  "reason": "why valid or not",\n'
        '  "corrected_reply": "improved reply using tool results if not valid, empty string if valid"\n'
        "}"
    )
    try:
        raw = _or_text(
            system=prompt,
            user="Validate.",
            max_tokens=300,
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

    full_system = (
        f"{system_prompt}\n\n"
        f"AVAILABLE TOOLS:\n{tools_desc}\n\n"
        f"CONVERSATION SO FAR:\n{history_text}"
        f"{plan_hint}\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"thought": "step-by-step reasoning", '
        '"tool_calls": [{"name": "tool_name", "args": {}}], '
        '"reply": "final message to owner when done", '
        '"done": true/false}\n'
        "Rules:\n"
        "- done=true ONLY when task is fully complete AND reply is non-empty\n"
        "- tool_calls=[] when replying directly with no tools needed\n"
        "- Never invent tool results — always call the tool\n"
        "- If KB search returns empty, use fallback_strategy — do NOT just say Done\n"
        "- Output ONLY valid JSON, no markdown fences"
    )

    user_content: list = []
    if image_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
        })
    if file_b64 and file_mime:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{file_mime};base64,{file_b64}"},
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
            desc = tdef.get("desc", "")[:100]
            lines.append(f"  {name}({args_str}) — {desc}")
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
    if len(result_str) <= MAX_TOOL_RESULT_CHARS:
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
                    if k in ("results", "items", "hits", "entries"):
                        compressed[k] = v[:10]
                    elif v and len(str(v[0])) < 200:
                        compressed[f"{k}_first"] = v[0]
                else:
                    compressed[k] = v
            out = json.dumps(compressed, default=str)
            if len(out) <= MAX_TOOL_RESULT_CHARS:
                return out
    except Exception:
        pass
    return result_str[:MAX_TOOL_RESULT_CHARS] + "…[truncated]"


def _execute_railway_tool(tool_name: str, tool_args: dict) -> str:
    try:
        from core_tools import TOOLS
        if tool_name not in TOOLS:
            return json.dumps({"ok": False, "error": f"tool '{tool_name}' not found"})
        fn     = TOOLS[tool_name]["fn"]
        result = fn(**tool_args) if tool_args else fn()
        raw    = json.dumps(result, default=str)
        return _compress_result(raw, tool_name)
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc()[:400]})


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
                f"select=status,result,error&id=eq.{task_id}&limit=1",
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
    "rm -rf", "destroy", "sb_delete", "permanent", "irreversible",
}


def _is_destructive(tool_name: str, tool_args: dict) -> bool:
    check = (tool_name + " " + json.dumps(tool_args, default=str)).lower()
    return any(k in check for k in _DESTRUCTIVE_KW)


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
        notify(text[:4096], cid=cid)
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
    """
    Sanitize tool result for model context injection.
    Injects explicit signals for failures and empty KB results.
    """
    try:
        parsed = json.loads(r)
        if isinstance(parsed, dict):
            ok    = parsed.get("ok", "?")
            parts = [f"ok={ok}"]
            for k in ["status", "summary", "output", "result", "error",
                      "count", "total", "commit", "path", "message"]:
                v = parsed.get(k)
                if v is not None:
                    parts.append(f"{k}={str(v)[:120]}")
            result_text = " | ".join(parts)

            # Explicit failure signal
            if ok is False or ok == "false" or parsed.get("error"):
                return f"TOOL_FAILED: {result_text}"

            # KB empty signal — model must use fallback strategy, not give up
            if tool_name in ("search_kb", "search_mistakes", "ask"):
                results = parsed.get("results") or parsed.get("items") or []
                count   = parsed.get("count", len(results) if isinstance(results, list) else -1)
                if count == 0 or results == []:
                    return (
                        f"KB_EMPTY: no results found for this query. "
                        f"Try: (1) different keywords, "
                        f"(2) sb_query table=hot_reflections/cold_reflections/pattern_frequency directly, "
                        f"(3) web_search if topic is external, "
                        f"(4) synthesize from session_start context already loaded. "
                        f"Do NOT declare done without answering."
                    )
            return result_text
    except Exception:
        pass
    return r.replace("\\", "/").replace('"', "'")[:400]


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
    history       = _get_history(cid)
    history_text  = _history_to_text(history)
    system_prompt = _build_system_prompt(cid)

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
            final = check.get("corrected_reply") or direct if not check.get("is_valid") else direct
            _tg_send(cid, final)
            _append_history(cid, "assistant", final)
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
            _sleep = 8 if last_tool in ("redeploy", "deploy_and_wait", "patch_file", "multi_patch") else 1
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
                "\n\nKB_MISS_ALERT: KB returned empty 2+ times. "
                "You MUST use the fallback_strategy from your pre-flight plan now. "
                "Options: sb_query direct tables, web_search, or synthesize from session_start data. "
                "Do NOT give up and do NOT call search_kb again with the same query."
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
                        max_tokens=400,
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
            else:
                _tg_send(cid, "✅ Done.")
            return

        # Execute tool calls
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args") or {}
            if not tool_name:
                continue

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

            results_buffer.append({
                "name":   tool_name,
                "result": _compress_result(result_str, tool_name),
            })

        # Stall detection
        if tool_calls and tool_call_count == _prev_count:
            final = reply or "✅ Done."
            _tg_send(cid, final)
            if reply:
                _append_history(cid, "assistant", reply)
            return

        if tool_calls:
            names = ", ".join(tc.get("name", "?") for tc in tool_calls)
            _tg_send(cid, f"⚙️ <i>{names}</i>")

        if done:
            final = reply.strip() if reply else ""
            if final:
                check = _validate_before_reply(user_message, final, results_buffer, system_prompt)
                if not check.get("is_valid", True) and check.get("corrected_reply"):
                    final = check["corrected_reply"].strip() or final
            _tg_send(cid, final or "✅ Done.")
            if final:
                _append_history(cid, "assistant", final)
            return

    _tg_send(
        cid,
        f"⚠️ Hit tool call limit ({MAX_TOOL_CALLS}). "
        "Task may be incomplete — send a follow-up to continue."
    )


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

    try:
        _agentic_loop(
            cid, text,
            image_b64=image_b64, image_mime=image_mime,
            file_b64=file_b64,   file_mime=file_mime,
        )
    except Exception as e:
        _tg_send(cid, f"❌ Error: {str(e)[:300]}")
        print(f"[ORCH] agentic_loop error:\n{traceback.format_exc()}")


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
                "select=content&domain=eq.system.config&topic=eq.supabase_pat&limit=1",
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
    notified: set = set()
    print("[ORCH] Desktop result poller started")
    while True:
        try:
            rows = sb_get(
                "task_queue",
                "select=id,status,result,error,chat_id"
                "&source=eq.mcp_session"
                "&status=in.(done,failed)"
                "&order=updated_at.desc&limit=20",
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
                if len(notified) > 500:
                    notified.clear()
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
