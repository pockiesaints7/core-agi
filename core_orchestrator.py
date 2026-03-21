"""
core_orchestrator.py — CORE Telegram Full-Power Agentic Orchestrator
=====================================================================
Token-optimised. Full-power. Model-agnostic.

OPTIMISATIONS vs naive approach:
  1. Dynamic tool selection   — cheap Groq call picks 10-15 relevant tools, not all 100+
  2. Tool result compression  — raw JSON → 1-line summary before entering context
  3. Rolling history summary  — every 10 turns compress to 1 summary, context never explodes
  4. Aggressive context cache — session_start cached 30 min (mostly static)
  5. Summary mode default     — step notifications go to Telegram but NOT into model context
  6. Lazy session_start       — loaded once per cache window, not every turn

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
  MODEL_PROVIDER = "gemini"       # now (free, Gemini keys already in env)
  MODEL_PROVIDER = "anthropic"    # when Anthropic API key ready
  MODEL_PROVIDER = "openai"       # fallback

VARIABLES VERIFIED AGAINST ACTUAL CODEBASE:
  core_config.py  : TELEGRAM_CHAT (not TELEGRAM_CHAT_ID), SUPABASE_PAT, SUPABASE_REF,
                    GROQ_FAST, sb_get(t, qs, svc), sb_post(t, d), sb_patch(t, m, d),
                    gemini_chat(system, user, max_tokens, json_mode)
  core_tools.py   : TOOLS dict keys are fn/perm/args/desc,
                    t_session_start() returns ok/counts/in_progress_tasks/pending_tasks/
                      domain_mistakes/top_patterns/quality_alert/behavioral_rules/live_tool_count,
                    t_get_behavioral_rules(domain, page, page_size) returns rules list
                      with fields trigger/pointer/full_rule/domain/priority
  core_main.py    : handle_msg(msg) uses cid/text, on_start() decorated with @app.on_event
"""

import base64
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Optional

import httpx

from core_config import (
    SUPABASE_URL, SUPABASE_SVC, SUPABASE_PAT, SUPABASE_REF,
    TELEGRAM_TOKEN, TELEGRAM_CHAT,      # TELEGRAM_CHAT — verified from core_config.py
    GROQ_FAST,                          # for cheap tool-selection call
    sb_get, sb_post, sb_patch,
    gemini_chat,                        # gemini_chat(system, user, max_tokens, json_mode)
)
from core_github import notify          # notify(msg, cid=None)

# ══════════════════════════════════════════════════════════════════════════════
# MODEL CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Primary provider is always OpenRouter (text + image + files).
# Fallback chain: OpenRouter → Gemini (direct) → Groq.
# MODEL_PROVIDER is kept for /model command display only — routing is automatic.
MODEL_PROVIDER = "openrouter"   # display label

# OpenRouter model — supports vision + long context
OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"   # swap to any OR model here

_MODEL_STRINGS = {
    "openrouter": OPENROUTER_MODEL,
    "gemini":     "gemini-2.5-flash-lite",
    "anthropic":  "claude-sonnet-4-20250514",
    "openai":     "gpt-4o",
    "groq":       "llama-3.3-70b-versatile",
}

# Telegram document MIME types → extension hint (for file naming only)
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
MAX_HISTORY_TURNS     = 20      # hard cap on turns kept in memory per chat
HISTORY_COMPRESS_AT   = 10      # compress oldest N turns into 1 summary when exceeded
MAX_TOOL_CALLS        = 50      # safety ceiling per message (unlimited intent)
MAX_TOOL_RESULT_CHARS = 800     # compress tool results beyond this in context
MAX_CONTEXT_CHARS     = 10000   # total system prompt chars fed to model
DESKTOP_TASK_TIMEOUT  = 300     # seconds to wait for PC task result
SESSION_CACHE_TTL     = 1800    # 30 min — session_start is mostly static
CONFIRM_TIMEOUT_SECS  = 120     # owner has this long to reply CONFIRM/REJECT

# ── In-memory state ────────────────────────────────────────────────────────────
_conv_memory: dict     = {}   # {cid: deque([{role, content, ts, image_b64?, image_mime?}])}
_conv_lock             = threading.Lock()
_pending_confirms: dict = {}  # {cid: {event, confirmed}}
_confirm_lock          = threading.Lock()
_session_cache: dict   = {}   # {cid: {system_prompt, loaded_at}}
_cache_lock            = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN-OPTIMISED TOOL SELECTION
# Cheap Groq call to pick 10-15 relevant tools before main model call.
# ~2000 tokens saved per turn vs injecting all 100+.
# ══════════════════════════════════════════════════════════════════════════════

# Tools that are ALWAYS included regardless of message (core infra)
_ALWAYS_TOOLS = {
    "session_end", "search_kb", "get_mistakes",
    "add_knowledge", "log_mistake", "notify_owner", "checkpoint",
    "task_add", "task_update", "sb_query", "sb_patch",
}

# Tool category map — keyword → tool names
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
    Use Groq fast model to pick relevant tool categories for this message.
    Returns list of tool names (always_tools + selected categories).
    Falls back to all tools if Groq fails.
    """
    try:
        from core_config import groq_chat, GROQ_FAST
        from core_tools import TOOLS

        all_tool_names = set(TOOLS.keys())
        categories_text = "\n".join(
            f"  {cat}: {', '.join(tools[:4])}..."
            for cat, tools in _TOOL_CATEGORIES.items()
        )
        raw = groq_chat(
            system=(
                "You are a tool router. Given a user message, output ONLY a JSON array "
                "of category names that are relevant. "
                f"Categories: {list(_TOOL_CATEGORIES.keys())}. "
                "Output only valid JSON array of strings, no preamble."
            ),
            user=f"Message: {message[:300]}\nHistory: {history_summary[:200]}",
            model=GROQ_FAST,
            max_tokens=60,
        )
        selected_cats = json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip())
        if not isinstance(selected_cats, list):
            raise ValueError("not a list")

        selected_tools = set(_ALWAYS_TOOLS)
        for cat in selected_cats:
            selected_tools.update(_TOOL_CATEGORIES.get(cat, []))
        # Only return tools that actually exist in TOOLS
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
    """
    Build full CORE system prompt. Cached per cid for SESSION_CACHE_TTL seconds.
    Mirrors Claude Desktop boot: session_start + behavioral_rules.
    Heavy but called at most once per 30 min per chat.
    """
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

    # session_start — same fields as Claude Desktop boot
    try:
        from core_tools import t_session_start
        ss = t_session_start()
        if ss.get("ok"):
            counts   = ss.get("counts", {})
            in_prog  = ss.get("in_progress_tasks", []) or []
            pending  = ss.get("pending_tasks", []) or []
            mistakes = ss.get("domain_mistakes", []) or []
            patterns = ss.get("top_patterns", []) or []
            qa       = ss.get("quality_alert")
            live_tools = ss.get("live_tool_count", 0)

            state_line = (
                f"STATE: KB={counts.get('knowledge_base',0)} "
                f"Sessions={counts.get('sessions',0)} "
                f"Mistakes={counts.get('mistakes',0)} "
                f"Tools={live_tools}"
            )
            parts.append(state_line)

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
                    f"{p.get('pattern','')[:80]}"
                    for p in patterns[:3]
                )
                parts.append(f"TOP PATTERNS: {p_lines}")

            if qa:
                parts.append(f"QUALITY ALERT: {qa}")

            # behavioral_rules already loaded by session_start
            rules = ss.get("behavioral_rules", []) or []
            if rules:
                r_lines = "\n".join(
                    f"  [{r.get('trigger','')}] {r.get('pointer','')[:100]}"
                    for r in rules[:15]
                )
                parts.append(f"BEHAVIORAL RULES:\n{r_lines}")

    except Exception as e:
        print(f"[ORCH] session_start error (non-fatal): {e}")

    # Railway tools summary (always visible in system prompt)
    parts.append(
        "RAILWAY TOOLS (no prefix — run on server instantly):\n"
        "  web_search(query, max_results) — search web via DuckDuckGo\n"
        "  web_fetch(url, max_chars) — fetch any URL content\n"
        "  summarize_url(url, focus) — fetch + Gemini summary\n"
        "  create_document(content, filename, format) — format=docx|pdf|txt|md|csv\n"
        "  create_spreadsheet(data, filename, format) — format=xlsx|csv\n"
        "  create_presentation(slides, filename, theme) — format=pptx\n"
        "  read_document(base64_content, format) — extract text from docx|xlsx|pptx|txt|csv\n"
        "  convert_document(base64_content, from_format, to_format) — convert between formats\n"
        "  generate_image(prompt, aspect_ratio) — Gemini Imagen\n"
        "  image_process(base64_content, operation, params) — resize|crop|rotate|watermark etc\n"
        "  weather(location) — current weather, default Jakarta\n"
        "  calc(expression) — safe math: sqrt, sin, log, pi, etc\n"
        "  datetime_now(timezone) — default Asia/Jakarta WIB\n"
        "  currency(amount, from_cur, to_cur) — live exchange rate\n"
        "  translate(text, target_language) — via Gemini\n"
        "  run_python(code, timeout) — execute Python on Railway"
    )

    # Desktop capabilities (PC tools — requires core_agent.py running on PC)
    parts.append(
        "DESKTOP TOOLS (prefix desktop_ — requires PC online):\n"
        "  desktop_run_script:  {script, lang: powershell|python}\n"
        "  desktop_file_ops:    {path, operation: read|write|list|delete|exists|move|mkdir|info|append, content?}\n"
        "  desktop_browser:     {url?, steps: [{action, selector?, value?, script?}], screenshot?}\n"
        "  desktop_search_web:  {query, max_results?}\n"
        "  desktop_cmd:         {command?, script?}"
    )

    # Constitution (immutable)
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
# CONVERSATION HISTORY — rolling summary compression
# ══════════════════════════════════════════════════════════════════════════════

def _sb_save_msg(cid: str, role: str, content: str):
    """Persist message to telegram_conversations (best-effort)."""
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
    """Load recent history from Supabase on cold start."""
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
    entry = {
        "role":    role,
        "content": content[:1500],
        "ts":      datetime.utcnow().isoformat(),
    }
    if image_b64:
        entry["image_b64"]  = image_b64
        entry["image_mime"] = image_mime or "image/jpeg"
    with _conv_lock:
        if cid not in _conv_memory:
            _conv_memory[cid] = deque(maxlen=MAX_HISTORY_TURNS)
        q = _conv_memory[cid]
        # Rolling compression: if near limit, compress oldest turns
        if len(q) >= MAX_HISTORY_TURNS - 2:
            _compress_history(q)
        q.append(entry)
    _sb_save_msg(cid, role, content)


def _compress_history(q: deque):
    """
    Compress oldest HISTORY_COMPRESS_AT entries into a single summary entry.
    Uses Groq fast model. Falls back to simple truncation if Groq fails.
    Mutates q in place.
    """
    if len(q) < HISTORY_COMPRESS_AT:
        return
    oldest = []
    for _ in range(HISTORY_COMPRESS_AT):
        if q:
            oldest.append(q.popleft())
    try:
        from core_config import groq_chat, GROQ_FAST
        text = "\n".join(
            f"{e['role'].upper()}: {e['content'][:200]}"
            for e in oldest
        )
        summary = groq_chat(
            system="Summarise this conversation segment in 2-3 sentences. Be factual, include outcomes.",
            user=text,
            model=GROQ_FAST,
            max_tokens=150,
        )
        q.appendleft({
            "role":    "system",
            "content": f"[HISTORY SUMMARY] {summary}",
            "ts":      oldest[0].get("ts", ""),
        })
    except Exception:
        # Fallback: just keep first and last of the compressed block
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
    """Convert history list to compact text for model context."""
    lines = []
    for h in history[-12:]:  # only last 12 turns in context
        role    = h.get("role", "user").upper()
        content = h.get("content", "")[:400]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ABSTRACTION LAYER
# ══════════════════════════════════════════════════════════════════════════════

def _call_model(system_prompt: str, history_text: str, user_message: str,
                tools_desc: str, image_b64: str = None,
                image_mime: str = "image/jpeg",
                file_b64: str = None, file_mime: str = None) -> dict:
    """
    Waterfall fallback chain:
      1. OpenRouter  (text + image + file — primary)
      2. Gemini direct (text + image — fallback)
      3. Groq        (text only — last resort)

    Returns:
      {"thought": str, "tool_calls": [{"name": str, "args": dict}],
       "reply": str, "done": bool}
    """
    errors = []

    # 1 — OpenRouter (handles text, image, and file attachments)
    try:
        return _call_openrouter(
            system_prompt, history_text, user_message,
            tools_desc, image_b64, image_mime, file_b64, file_mime,
        )
    except Exception as e:
        err = str(e)
        errors.append(f"OpenRouter: {err[:200]}")
        print(f"[ORCH] OpenRouter failed, falling back to Gemini: {err[:200]}")

    # 2 — Gemini direct (text + image, no arbitrary file)
    try:
        return _call_gemini(
            system_prompt, history_text, user_message,
            tools_desc, image_b64, image_mime,
        )
    except Exception as e:
        err = str(e)
        errors.append(f"Gemini: {err[:200]}")
        print(f"[ORCH] Gemini failed, falling back to Groq: {err[:200]}")

    # 3 — Groq (text only — no vision, but keeps the loop alive)
    try:
        return _call_groq_model(
            system_prompt, history_text, user_message, tools_desc,
        )
    except Exception as e:
        errors.append(f"Groq: {str(e)[:200]}")

    raise RuntimeError("All providers failed:\n" + "\n".join(errors))


def _call_openrouter(system_prompt: str, history_text: str, user_message: str,
                     tools_desc: str, image_b64: str = None,
                     image_mime: str = "image/jpeg",
                     file_b64: str = None, file_mime: str = None) -> dict:
    """
    OpenRouter via /v1/chat/completions (OpenAI-compatible).
    Supports: text, images (inline base64), and arbitrary files (PDF, DOCX, etc.)
    via base64 data URIs in the content array.
    Tool calling via structured JSON output (same schema as Gemini path).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    # Build system message
    full_system = (
        f"{system_prompt}\n\n"
        f"AVAILABLE TOOLS:\n{tools_desc}\n\n"
        f"CONVERSATION SO FAR:\n{history_text}\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"thought": "step-by-step reasoning", '
        '"tool_calls": [{"name": "tool_name", "args": {}}], '
        '"reply": "final message to owner when done", '
        '"done": true/false}\n'
        "Rules:\n"
        "- done=true ONLY when task is fully complete and reply is set\n"
        "- tool_calls=[] when replying directly with no tools needed\n"
        "- Never invent tool results — always call the tool\n"
        "- Output ONLY valid JSON, no markdown fences"
    )

    # Build user content — supports text + image + file (all as inline data URIs)
    user_content: list = []

    if image_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
        })

    if file_b64 and file_mime:
        # For PDFs and docs — embed as data URI; models that support it will parse inline
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{file_mime};base64,{file_b64}"},
        })

    user_content.append({"type": "text", "text": f"OWNER: {user_message}"})

    payload = {
        "model":       OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": full_system},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens":  2048,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization":  f"Bearer {api_key}",
            "Content-Type":   "application/json",
            "HTTP-Referer":   "https://core-agi-production.up.railway.app",
            "X-Title":        "CORE AGI",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("429 rate limited")
    r.raise_for_status()
    data       = r.json()
    choice     = data["choices"][0]
    msg        = choice["message"]
    finish     = choice.get("finish_reason", "")
    raw_text   = msg.get("content") or "{}"
    try:
        parsed = json.loads(raw_text.strip().lstrip("```json").lstrip("```").rstrip("```").strip())
    except Exception:
        # Model returned plain text — treat as final reply
        return {"thought": "", "tool_calls": [], "reply": raw_text, "done": True}

    return {
        "thought":    parsed.get("thought", ""),
        "tool_calls": parsed.get("tool_calls", []),
        "reply":      parsed.get("reply", ""),
        "done":       bool(parsed.get("done", False)),
    }


def _call_groq_model(system_prompt: str, history_text: str, user_message: str,
                     tools_desc: str) -> dict:
    """
    Groq fast model — text only, last-resort fallback.
    Re-uses groq_chat() from core_config which handles 429 + key rotation.
    """
    from core_config import groq_chat, GROQ_FAST

    full_prompt = (
        f"{system_prompt}\n\n"
        f"AVAILABLE TOOLS:\n{tools_desc}\n\n"
        f"CONVERSATION SO FAR:\n{history_text}\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"thought": "reasoning", '
        '"tool_calls": [{"name": "tool_name", "args": {}}], '
        '"reply": "final reply when done", '
        '"done": true/false}\n'
        "Output ONLY valid JSON, no markdown fences."
    )
    raw = groq_chat(
        system=full_prompt,
        user=f"OWNER: {user_message}",
        model=GROQ_FAST,
        max_tokens=1024,
    )
    try:
        parsed = json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip())
    except Exception:
        return {"thought": "", "tool_calls": [], "reply": raw, "done": True}
    return {
        "thought":    parsed.get("thought", ""),
        "tool_calls": parsed.get("tool_calls", []),
        "reply":      parsed.get("reply", ""),
        "done":       bool(parsed.get("done", False)),
    }


def _call_gemini(system_prompt: str, history_text: str, user_message: str,
                 tools_desc: str, image_b64: str = None,
                 image_mime: str = "image/jpeg") -> dict:
    """
    Gemini via generateContent. Tool calling via structured JSON output.
    Uses gemini_chat() from core_config which already handles key rotation + 429 fallback.
    NOTE: gemini_chat() combines system+user into one prompt — we build accordingly.
    """
    full_system = (
        f"{system_prompt}\n\n"
        f"AVAILABLE TOOLS:\n{tools_desc}\n\n"
        f"CONVERSATION SO FAR:\n{history_text}\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"thought": "step-by-step reasoning", '
        '"tool_calls": [{"name": "tool_name", "args": {}}], '
        '"reply": "final message to owner when done", '
        '"done": true/false}\n'
        "Rules:\n"
        "- done=true ONLY when task is fully complete and reply is set\n"
        "- tool_calls=[] when replying directly with no tools needed\n"
        "- Never invent tool results — always call the tool\n"
        "- Output ONLY valid JSON, no markdown fences"
    )

    user_part = f"OWNER: {user_message}"

    # If image attached, we can't pass it via gemini_chat() (which takes text only).
    # Call the API directly for image turns, reuse gemini_chat() for text turns.
    if image_b64:
        from core_config import _GEMINI_KEYS, _GEMINI_KEY_INDEX
        import core_config as _cc
        keys = _cc._GEMINI_KEYS
        if not keys:
            raise RuntimeError("GEMINI_KEYS not set")
        model_name = _MODEL_STRINGS["gemini"]
        combined   = f"{full_system}\n\n{user_part}"
        parts_list = [
            {"text": combined},
            {"inline_data": {"mime_type": image_mime, "data": image_b64}},
        ]
        last_err = None
        for _ in range(len(keys)):
            key = keys[_cc._GEMINI_KEY_INDEX % len(keys)]
            _cc._GEMINI_KEY_INDEX = (_cc._GEMINI_KEY_INDEX + 1) % len(keys)
            try:
                r = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                    params={"key": key},
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": parts_list}],
                        "generationConfig": {
                            "maxOutputTokens": 2048,
                            "temperature": 0.1,
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
                parsed = json.loads(raw.lstrip("```json").lstrip("```").rstrip("```").strip())
                return {
                    "thought":    parsed.get("thought", ""),
                    "tool_calls": parsed.get("tool_calls", []),
                    "reply":      parsed.get("reply", ""),
                    "done":       bool(parsed.get("done", False)),
                }
            except Exception as e:
                last_err = str(e)
                continue
        raise RuntimeError(f"Gemini image call failed: {last_err}")
    else:
        # Text-only — use gemini_chat() which handles rotation + 429 automatically
        raw = gemini_chat(
            system=full_system,
            user=user_part,
            max_tokens=2048,
            json_mode=True,
        )
        parsed = json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip())
        return {
            "thought":    parsed.get("thought", ""),
            "tool_calls": parsed.get("tool_calls", []),
            "reply":      parsed.get("reply", ""),
            "done":       bool(parsed.get("done", False)),
        }


def _call_anthropic(system_prompt: str, history_text: str, user_message: str,
                    tools_desc: str, image_b64: str = None,
                    image_mime: str = "image/jpeg") -> dict:
    """Anthropic Claude via /v1/messages with native tool use."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    # Build tools from TOOLS dict
    try:
        from core_tools import TOOLS
        selected_names = [t.strip() for t in tools_desc.split("\n")
                          if t.strip() and not t.strip().startswith("desktop_")]
        anth_tools = []
        for name, tdef in TOOLS.items():
            props = {}
            for arg in (tdef.get("args") or []):
                an = arg["name"] if isinstance(arg, dict) else arg
                at = arg.get("type", "string") if isinstance(arg, dict) else "string"
                props[an] = {"type": at}
            anth_tools.append({
                "name":         name,
                "description":  tdef.get("desc", name)[:200],
                "input_schema": {"type": "object", "properties": props},
            })
    except Exception:
        anth_tools = []

    # Build message content
    user_content: list = []
    if image_b64:
        user_content.append({"type": "image", "source": {
            "type": "base64", "media_type": image_mime, "data": image_b64,
        }})
    user_content.append({"type": "text", "text": (
        f"CONVERSATION SO FAR:\n{history_text}\n\nOWNER: {user_message}"
    )})

    payload: dict = {
        "model":      _MODEL_STRINGS["anthropic"],
        "max_tokens": 4096,
        "system":     f"{system_prompt}\n\nAVAILABLE TOOLS:\n{tools_desc}",
        "messages":   [{"role": "user", "content": user_content}],
    }
    if anth_tools:
        payload["tools"] = anth_tools[:64]

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data       = r.json()
    content    = data.get("content", [])
    stop       = data.get("stop_reason", "")
    text_parts = [b["text"] for b in content if b.get("type") == "text"]
    tool_uses  = [
        {"name": b["name"], "args": b.get("input", {})}
        for b in content if b.get("type") == "tool_use"
    ]
    reply_text = " ".join(text_parts)
    return {
        "thought":    "",
        "tool_calls": tool_uses,
        "reply":      reply_text,
        "done":       stop == "end_turn" and not tool_uses,
    }


def _call_openai(system_prompt: str, history_text: str, user_message: str,
                 tools_desc: str, image_b64: str = None,
                 image_mime: str = "image/jpeg") -> dict:
    """OpenAI GPT-4o via /v1/chat/completions."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    user_content: list = []
    if image_b64:
        user_content.append({"type": "image_url", "image_url": {
            "url": f"data:{image_mime};base64,{image_b64}"
        }})
    user_content.append({"type": "text", "text": (
        f"CONVERSATION:\n{history_text}\n\nOWNER: {user_message}"
    )})

    # Build function tools
    try:
        from core_tools import TOOLS
        oai_tools = []
        for name, tdef in TOOLS.items():
            props = {}
            for arg in (tdef.get("args") or []):
                an = arg["name"] if isinstance(arg, dict) else arg
                at = arg.get("type", "string") if isinstance(arg, dict) else "string"
                props[an] = {"type": at}
            oai_tools.append({
                "type": "function",
                "function": {
                    "name":        name,
                    "description": tdef.get("desc", name)[:200],
                    "parameters":  {"type": "object", "properties": props},
                },
            })
    except Exception:
        oai_tools = []

    payload: dict = {
        "model":       _MODEL_STRINGS["openai"],
        "messages":    [
            {"role": "system", "content": f"{system_prompt}\n\nTOOLS:\n{tools_desc}"},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens":  4096,
        "temperature": 0.1,
    }
    if oai_tools:
        payload["tools"]       = oai_tools[:64]
        payload["tool_choice"] = "auto"

    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data       = r.json()
    choice     = data["choices"][0]
    msg        = choice["message"]
    finish     = choice.get("finish_reason", "")
    text       = msg.get("content") or ""
    tool_calls = []
    for tc in msg.get("tool_calls", []):
        try:
            args = json.loads(tc["function"]["arguments"])
        except Exception:
            args = {}
        tool_calls.append({"name": tc["function"]["name"], "args": args})
    return {
        "thought":    "",
        "tool_calls": tool_calls,
        "reply":      text,
        "done":       finish == "stop" and not tool_calls,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS DESCRIPTION — compact, used in model context
# ══════════════════════════════════════════════════════════════════════════════

def _build_tools_desc(selected_tool_names: list) -> str:
    """Build compact tool descriptions for selected tools + desktop tools."""
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

    # Always include desktop tools description
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
    """
    Compress tool result to MAX_TOOL_RESULT_CHARS for context efficiency.
    For JSON results, extract the most signal-rich fields.
    """
    if len(result_str) <= MAX_TOOL_RESULT_CHARS:
        return result_str
    try:
        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            # Keep ok, error, key counts, summaries — drop fat arrays
            compressed = {}
            for k, v in parsed.items():
                if k in ("ok", "error", "error_code", "message", "status",
                         "count", "total", "applied", "inserted", "summary",
                         "result", "output", "commit", "path", "version"):
                    compressed[k] = v
                elif isinstance(v, list):
                    compressed[f"{k}_count"] = len(v)
                    # For search/query results, keep all items not just first
                    if k in ("results", "items", "hits", "entries"):
                        compressed[k] = v[:10]  # keep up to 10
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
    """Direct call into TOOLS dict. Returns compressed result string."""
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
    """Queue desktop task to core_agent.py on PC, wait for result."""
    action = tool_name.replace("desktop_", "", 1)

    # Parse steps from JSON string if browser
    if action == "browser":
        if isinstance(tool_args.get("steps"), str):
            try:
                tool_args["steps"] = json.loads(tool_args["steps"])
            except Exception:
                pass
        if isinstance(tool_args.get("screenshot"), str):
            tool_args["screenshot"] = tool_args["screenshot"].lower() == "true"

    try:
        # task JSON blob — "desktop_agent": true is the marker core_agent.py uses to filter
        # source must be a valid enum: mcp_session|self_assigned|core_v6_registry|bulk_apply|improvement
        # status valid values: pending|in_progress|done|failed
        task_payload = json.dumps({
            "desktop_agent": True,      # filter key — core_agent.py checks this
            "action":        action,
            "payload":       tool_args,
            "chat_id":       cid,
            "queued_at":     datetime.utcnow().isoformat(),
        })
        ok = sb_post("task_queue", {
            "task":     task_payload,
            "status":   "pending",
            "priority": 9,
            "source":   "mcp_session",  # valid enum value
            "chat_id":  cid,
        })
        if not ok:
            return json.dumps({"ok": False, "error": "sb_post failed for task_queue"})

        # Get task id — source=mcp_session (valid enum), filter by chat_id + pending
        rows = sb_get(
            "task_queue",
            f"select=id"
            f"&source=eq.mcp_session"
            f"&status=eq.pending"
            f"&chat_id=eq.{cid}"
            f"&order=created_at.desc"
            f"&limit=1",
        ) or []
        if not rows:
            return json.dumps({"ok": False, "error": "task queued but id not found"})
        task_id = str(rows[0]["id"])

        # Poll — valid status values: pending|in_progress|done|failed
        deadline = time.time() + DESKTOP_TASK_TIMEOUT
        while time.time() < deadline:
            r = sb_get(
                "task_queue",
                f"select=status,result,error&id=eq.{task_id}&limit=1",
            ) or []
            if r:
                status = r[0].get("status")
                if status == "done":
                    out = r[0].get("result") or "Done."
                    return _compress_result(out, tool_name)
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
    """Consume a CONFIRM/REJECT reply. Returns True if consumed."""
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
    """Ask owner to confirm. Blocks current thread until reply or timeout."""
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
    """Send text message. Uses notify() from core_github which handles TELEGRAM_TOKEN."""
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
    """Send base64 image as photo."""
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
    """Download any Telegram file (photo or document), return as base64 string."""
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
        print(f"[ORCH] _tg_download_photo error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AGENTIC LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _agentic_loop(cid: str, user_message: str,
                  image_b64: str = None, image_mime: str = "image/jpeg",
                  file_b64: str = None, file_mime: str = None):
    """
    Full agentic loop with token optimisations:
    1. Select relevant tools (cheap Groq call)
    2. Build compact tools description
    3. Load cached session context
    4. Call model with compressed history + user message
    5. Execute tool calls → compress results → feed back to model
    6. Stream thought + step notifications to Telegram
    7. Loop until done=True or ceiling hit
    """
    history          = _get_history(cid)
    history_text     = _history_to_text(history)
    system_prompt    = _build_system_prompt(cid)
    selected_tools   = _select_tools(user_message, history_text)
    tools_desc       = _build_tools_desc(selected_tools)
    tool_call_count  = 0
    _prev_count      = 0
    # Accumulate tool results in a separate buffer (not full history)
    # This is the key token optimisation: results don't bloat history
    results_buffer: list = []
    # Loop detection: track (tool_name, frozen_args) to catch infinite repeats
    _seen_calls: set = set()

    while tool_call_count < MAX_TOOL_CALLS:
        _prev_count = tool_call_count
        if tool_call_count > 0:
            time.sleep(3)
        _tg_typing(cid)

        # Build user content for this loop iteration
        if results_buffer:
            def _safe_result(r: str) -> str:
                """Sanitize tool result before injecting into Gemini prompt.
                Raw JSON with backslashes breaks Gemini's JSON output mode.
                Parse to plain text summary instead."""
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
                        return " | ".join(parts)
                except Exception:
                    pass
                # Fallback: strip chars that break Gemini JSON output
                return r.replace("\\", "/").replace('"', "'")[:400]

            tool_results_text = "\n".join(
                f"[{r['name']}] → {_safe_result(r['result'])}"
                for r in results_buffer[-5:]
            )
            current_user = (
                f"{user_message}\n\n"
                f"TOOL RESULTS SO FAR:\n{tool_results_text}"
            )
        else:
            current_user = user_message

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
            )
        except Exception as e:
            err = str(e)
            _tg_send(cid, f"❌ All providers failed: {err[:300]}")
            return

        thought    = response.get("thought", "")
        tool_calls = response.get("tool_calls", [])
        reply      = response.get("reply", "")
        done       = response.get("done", False)

        # Thoughts are suppressed — too noisy per turn

        # No tool calls — model is done or stuck
        if not tool_calls:
            if reply:
                _tg_send(cid, reply)
                _append_history(cid, "assistant", reply)
            elif results_buffer:
                # Model has results but gave no reply — summarise and exit
                last = results_buffer[-1]
                _tg_send(cid, f"✅ {last['name']}: {last['result'][:300]}")
            else:
                _tg_send(cid, "✅ Done.")
            return

        # Execute tool calls
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args") or {}
            if not tool_name:
                continue

            # Loop detection — skip exact duplicate calls within this agentic turn
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
            if call_key in _seen_calls:
                # Inject a fake "already done" result so model moves on
                results_buffer.append({
                    "name":   tool_name,
                    "result": '{"ok": true, "note": "already called this turn — result cached above"}',
                })
                continue
            _seen_calls.add(call_key)

            tool_call_count += 1

            # Step notification suppressed — batched summary sent after round

            # Destructive gate
            if _is_destructive(tool_name, tool_args):
                confirmed = _request_confirmation(cid, tool_name, tool_args)
                if not confirmed:
                    results_buffer.append({
                        "name": tool_name,
                        "result": '{"ok": false, "error": "CANCELLED by owner"}',
                    })
                    _tg_send(cid, "🚫 Action cancelled.")
                    continue

            # Execute
            is_desktop = tool_name.startswith("desktop_")
            if is_desktop:
                _tg_send(cid, "🖥 <i>Sending to PC...</i>")
                result_str = _execute_desktop_tool(tool_name, tool_args, cid)
            else:
                result_str = _execute_railway_tool(tool_name, tool_args)

            # Individual result preview suppressed — see round summary below

            # Auto-send screenshot if result contains base64 image
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

            # Store in results buffer (compressed)
            results_buffer.append({
                "name":   tool_name,
                "result": _compress_result(result_str, tool_name),
            })

        # One summary per round — not per tool
        if tool_calls:
            names = ", ".join(tc.get("name","?") for tc in tool_calls)
            _tg_send(cid, f"⚙️ <i>{names}</i>")

        # Stall detection: if no new tool_call_count was incremented this round,
        # every call was a duplicate — model is stuck, force exit
        if tool_calls and tool_call_count == _prev_count:
            if reply:
                _tg_send(cid, reply)
                _append_history(cid, "assistant", reply)
            else:
                _tg_send(cid, "✅ Done.")
            return

        if done:
            if reply:
                _tg_send(cid, reply)
                _append_history(cid, "assistant", reply)
            return

    # Safety ceiling
    _tg_send(
        cid,
        f"⚠️ Hit tool call limit ({MAX_TOOL_CALLS}). "
        "Task may be incomplete — send a follow-up to continue."
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — called by core_main.py handle_msg()
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

    # Security: owner only
    if cid != str(TELEGRAM_CHAT):
        _tg_send(cid, "Unauthorized.")
        return

    # Consume CONFIRM/REJECT replies first
    if text and handle_confirm_reply(cid, text):
        return

    # Strip bot @username suffix from commands
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
            f"Fallback chain: OpenRouter → Gemini → Groq\n"
            f"Swap: change OPENROUTER_MODEL in core_orchestrator.py"
        )
        return

    if lower in ("/refresh", "refresh"):
        _invalidate_cache(cid)
        _tg_send(cid, "🔄 Session cache cleared — reloads on next message.")
        return

    # ── Attachment handling ─────────────────────────────────────────────────
    image_b64  = None
    image_mime = "image/jpeg"
    file_b64   = None
    file_mime  = None
    file_name  = None

    # Photo (Telegram compressed image)
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

    # Document / file (PDF, DOCX, XLSX, TXT, images sent as file, etc.)
    elif doc:
        file_id   = doc.get("file_id")
        file_mime = doc.get("mime_type") or "application/octet-stream"
        file_name = doc.get("file_name") or "attachment"
        file_size = doc.get("file_size") or 0

        if file_size > 20 * 1024 * 1024:  # 20 MB hard cap
            _tg_send(cid, "❌ File too large (max 20 MB).")
            return

        _tg_send(cid, f"📎 Downloading <b>{file_name}</b> ({file_size // 1024} KB)...")
        raw_b64 = _tg_download_file(file_id)
        if not raw_b64:
            _tg_send(cid, "❌ Failed to download file.")
            return

        # Images sent as document — treat as image for vision models
        if file_mime.startswith("image/"):
            image_b64  = raw_b64
            image_mime = file_mime
        else:
            file_b64 = raw_b64
            # file_mime already set above

        if not text:
            ext  = _TG_MIME_EXT.get(file_mime, "file")
            text = f"I've sent you a {ext.upper()} file named '{file_name}'. Please read and analyse it."

    if not text and not image_b64 and not file_b64:
        return

    print(f"[ORCH] [{cid}] {text[:80]}"
          + (f" [img]" if image_b64 else "")
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
    """
    Auto-create telegram_conversations via Supabase Management API.
    Reads SUPABASE_PAT from core_config (already loaded as env var).
    Called in background thread at startup — non-blocking, non-fatal.
    """
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
        # SUPABASE_PAT is already in core_config env — use directly
        pat = SUPABASE_PAT
        if not pat:
            print("[ORCH] _ensure_table: SUPABASE_PAT not set — trying KB fallback")
            rows = sb_get(
                "knowledge_base",
                "select=content&domain=eq.system.config&topic=eq.supabase_pat&limit=1",
                svc=True,
            )
            pat = (rows[0].get("content", "") if rows else "").strip()
        if not pat:
            print("[ORCH] _ensure_table: no PAT available — skipping auto-migration")
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
# Catches tasks that timed out inline but completed later on PC.
# ══════════════════════════════════════════════════════════════════════════════

def _desktop_result_poller():
    """
    Watch for completed desktop tasks that have a chat_id.
    Notifies owner if task finished after the inline wait timed out.
    """
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
# STARTUP — called by core_main.py on_start()
# ══════════════════════════════════════════════════════════════════════════════

def start_orchestrator():
    """
    Start all orchestrator background threads.
    Call this from core_main.py on_start() after the existing thread starts.
    """
    threading.Thread(target=_ensure_table,          daemon=True, name="orch_ensure_table").start()
    threading.Thread(target=_desktop_result_poller, daemon=True, name="orch_result_poller").start()
    print(f"[ORCH] Started. Provider: {MODEL_PROVIDER} ({_MODEL_STRINGS.get(MODEL_PROVIDER,'?')})")
