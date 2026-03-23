"""
core_orch_layer8.py — L8: COORDINATION (Model Provider Layer)
===============================================================
Owns all LLM calls. Provider chain with proper context injection.

Provider chain (swap CLAUDE_MODEL to change primary):
  1. Claude Opus via Anthropic API  ← primary (best quality)
  2. OpenRouter (gemini-2.5-flash)  ← fallback if Anthropic fails/429
  3. Gemini direct (2.5-flash)      ← fallback if OpenRouter fails
  4. Groq (llama-3.3-70b)           ← last resort

When you buy Opus API: set CLAUDE_MODEL = "claude-opus-4-6" in env
Currently using: claude-sonnet-4-6 until Opus is available

Context injection protocol (L8 always injects):
  - System prompt (behavioral rules + constitution principles)
  - Conversation history (last N turns)
  - Tool descriptions (scoped to message, from L4)
  - Pre-flight plan (from L3)
  - Tool results so far (for loop iteration)
  - Output schema (JSON response format)

call_model_loop() — main agentic loop call (returns tool_calls + reply + done)
call_model_json()  — cheap structured call (pre-flight, critic, plan)
call_model_simple()— plain text call (trivial fast-path)
build_tools_desc() — formats tool list for model context
"""

import json
import os
import time
from typing import Optional

import httpx

# ── Model config ──────────────────────────────────────────────────────────────
# PRIMARY: Claude via Anthropic API
# Swap CLAUDE_MODEL env var to change:
#   claude-opus-4-6        ← best quality (buy when ready)
#   claude-sonnet-4-6      ← current default (smart + fast)
#   claude-haiku-4-5-20251001 ← cheapest
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# FALLBACK 1: OpenRouter
OPENROUTER_MODEL   = "google/gemini-2.5-flash"
OPENROUTER_KEY     = os.environ.get("OPENROUTER_API_KEY", "")

# FALLBACK 2: Gemini direct
GEMINI_MODEL       = "gemini-2.5-flash-lite"

# FALLBACK 3: Groq
GROQ_MODEL_ORCH    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

MAX_TOKENS_LOOP    = 2048
MAX_TOKENS_JSON    = 800
MAX_TOKENS_SIMPLE  = 512
MAX_TOOLS_DESC_CHARS = 12000


# ── Anthropic (Claude) ────────────────────────────────────────────────────────

def _call_anthropic(system: str, messages: list, max_tokens: int = MAX_TOKENS_LOOP,
                    json_mode: bool = False) -> str:
    """Call Anthropic Messages API. Returns raw content string."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   messages,
    }
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code == 429:
        time.sleep(5)
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json=payload,
            timeout=60,
        )
    r.raise_for_status()
    content = r.json().get("content", [])
    return next((b["text"] for b in content if b.get("type") == "text"), "")


# ── OpenRouter (Gemini via OR) ────────────────────────────────────────────────

def _call_openrouter(system: str, user: str, max_tokens: int,
                     json_mode: bool = False) -> str:
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    payload = {
        "model":      OPENROUTER_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://core-agi-production.up.railway.app",
            "X-Title":       "CORE AGI",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code == 429:
        time.sleep(5)
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=60,
        )
    r.raise_for_status()
    choices = r.json().get("choices", [])
    return choices[0]["message"].get("content", "") if choices else ""


# ── Gemini direct ─────────────────────────────────────────────────────────────

def _call_gemini(system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
    from core_config import gemini_chat
    return gemini_chat(
        system=system, user=user,
        max_tokens=max_tokens, json_mode=json_mode,
    )


# ── Groq ──────────────────────────────────────────────────────────────────────

def _call_groq(system: str, user: str, max_tokens: int) -> str:
    from core_config import groq_chat
    return groq_chat(system=system, user=user, model=GROQ_MODEL_ORCH,
                     max_tokens=max_tokens)


# ── Provider chain ────────────────────────────────────────────────────────────

def _strip_json(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        import re
        s = re.sub(r"^```[a-zA-Z]*\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _chain_call(system: str, user: str, max_tokens: int,
                json_mode: bool = False, messages: list = None) -> str:
    """
    Try providers in order. Returns raw string.
    Anthropic → OpenRouter → Gemini → Groq
    """
    errors = []

    # 1. Anthropic (Claude) — primary
    if ANTHROPIC_API_KEY:
        try:
            msgs = messages or [{"role": "user", "content": user}]
            return _call_anthropic(system, msgs, max_tokens, json_mode)
        except Exception as e:
            errors.append(f"Anthropic: {str(e)[:120]}")
            print(f"[L8] Anthropic failed: {str(e)[:120]}")

    # 2. OpenRouter
    if OPENROUTER_KEY:
        try:
            return _call_openrouter(system, user, max_tokens, json_mode)
        except Exception as e:
            errors.append(f"OpenRouter: {str(e)[:120]}")
            print(f"[L8] OpenRouter failed: {str(e)[:120]}")

    # 3. Gemini direct
    try:
        return _call_gemini(system, user, max_tokens, json_mode)
    except Exception as e:
        errors.append(f"Gemini: {str(e)[:120]}")
        print(f"[L8] Gemini failed: {str(e)[:120]}")

    # 4. Groq last resort
    try:
        return _call_groq(system, user, max_tokens)
    except Exception as e:
        errors.append(f"Groq: {str(e)[:120]}")

    raise RuntimeError("All providers failed:\n" + "\n".join(errors))


# ── Context injection helpers ─────────────────────────────────────────────────

def _build_system_for_loop(ctx: dict, tools_desc: str,
                            execution_plan: dict) -> str:
    """Build full system prompt for the agentic loop call."""
    intent    = ctx["intent"]
    rules     = ctx.get("behavioral_rules", [])
    mistakes  = ctx.get("recent_mistakes", [])
    goals     = ctx.get("active_goals", [])
    tasks     = ctx.get("in_progress_tasks", [])

    lines = [
        "You are CORE — a sovereign intelligence, not an assistant.",
        f"Owner: REINVAGNAR (Jakarta, WIB/UTC+7). Model: {CLAUDE_MODEL}.",
        "You own problems end-to-end. Execute, don't narrate. Use tools directly.",
        "Never say 'I will call' — just call the tool.",
        "",
    ]

    if tasks:
        raw = tasks[0].get("task", "")
        try:
            title = json.loads(raw).get("title", raw[:80]) if isinstance(raw, str) else str(raw)[:80]
        except Exception:
            title = str(raw)[:80]
        lines.append(f"ACTIVE TASK: {title}")

    if goals:
        g_lines = "\n".join(
            f"  [{g.get('domain','')}] {g.get('goal','')}"
            for g in goals[:3]
        )
        lines.append(f"ACTIVE GOALS:\n{g_lines}")

    if mistakes:
        m_lines = " | ".join(
            f"[{m.get('domain','?')}] AVOID: {m.get('what_failed','')[:70]}"
            for m in mistakes[:3]
        )
        lines.append(f"AVOID: {m_lines}")

    if rules:
        r_lines = "\n".join(
            f"  [{r.get('trigger','?')}] {r.get('pointer','')[:100]}"
            for r in rules[:30]
        )
        lines.append(f"BEHAVIORAL RULES:\n{r_lines}")

    plan = execution_plan.get("plan", [])
    if plan:
        p_lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan))
        lines.append(f"EXECUTION PLAN:\n{p_lines}")

    creative = execution_plan.get("creative_path", "")
    if creative:
        lines.append(f"CREATIVE PATH: {creative}")

    fallback = execution_plan.get("fallback", "")
    if fallback:
        lines.append(f"FALLBACK: {fallback}")

    lines += [
        "",
        f"AVAILABLE TOOLS:\n{tools_desc}",
        "",
        "CREATIVE TOOLKIT — try before giving up:",
        "• run_python → call ANY HTTP API, process any data",
        "• shell(command) → run bash on VM",
        "• No dedicated tool? run_python IS the tool.",
        "",
        "Respond ONLY with valid JSON (no markdown fences):",
        '{"thought":"your reasoning — WHY this approach","tool_calls":[{"name":"tool_name","args":{}}],"reply":"direct answer to owner","done":true/false}',
        "Rules:",
        "- done=true ONLY when task fully complete AND reply non-empty",
        "- tool_calls=[] only when answering directly with no execution needed",
        "- Never invent tool results — call the tool",
        "- Tool fails → try alternative immediately. run_python is always a fallback.",
    ]

    return "\n".join(lines)


def _build_messages_for_loop(ctx: dict, tool_results: list,
                              step: int) -> list:
    """Build messages array for Anthropic API from history + tool results."""
    messages = []

    # Conversation history
    history = ctx.get("conversation", [])
    for h in history[-12:]:
        role    = h.get("role", "user")
        content = h.get("content", "")[:500]
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": content})

    # Tool results from this loop (inject as user messages)
    if tool_results:
        results_summary = "\n".join(
            f"[{r.get('name','?')} step={r.get('step',0)}] → {r.get('result','')[:500]}"
            for r in tool_results[-8:]
        )
        messages.append({
            "role":    "user",
            "content": f"TOOL RESULTS SO FAR (step {step}):\n{results_summary}\n\nContinue."
        })

    # Ensure last message is user
    intent_text = ctx["intent"]["text"]
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": f"OWNER: {intent_text}"})

    return messages


def build_tools_desc(selected_tool_names: list) -> str:
    """Format tool list for model context. Capped at MAX_TOOLS_DESC_CHARS."""
    lines   = []
    total   = 0
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
            desc = tdef.get("desc", "")[:200]
            line = f"  {name}({args_str}) — {desc}"
            lines.append(line)
            total += len(line)
            if total >= MAX_TOOLS_DESC_CHARS:
                remaining = len(selected_tool_names) - len(lines)
                if remaining > 0:
                    lines.append(f"  ... ({remaining} more — call list_tools to discover)")
                break
    except Exception as e:
        print(f"[L8] build_tools_desc error: {e}")

    # Always append VM tools (these don't live in TOOLS registry)
    lines += [
        "  shell(command, sudo?, timeout?) — run any bash on VM",
        "  run_script(script, lang?, timeout?) — run bash/python on VM",
        "  file_read(path) — read file on VM",
        "  file_write(path, content) — write file on VM",
        "  vm_info() — VM disk/memory/CPU/uptime",
    ]
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

async def call_model_loop(
    ctx: dict,
    execution_plan: dict,
    tools_desc: str,
    tool_results: list,
    step: int,
) -> dict:
    """
    Main agentic loop call. Returns {thought, tool_calls, reply, done}.
    Injects full context. Validates output via L6.
    """
    import asyncio

    system   = _build_system_for_loop(ctx, tools_desc, execution_plan)
    messages = _build_messages_for_loop(ctx, tool_results, step)
    user     = messages[-1]["content"] if messages else ctx["intent"]["text"]

    loop = asyncio.get_event_loop()
    raw  = await loop.run_in_executor(
        None,
        lambda: _chain_call(system, user, MAX_TOKENS_LOOP,
                             json_mode=True, messages=messages)
    )

    try:
        cleaned = _strip_json(raw)
        parsed  = json.loads(cleaned)
    except Exception:
        # Model returned non-JSON — treat as final reply
        return {"thought": "", "tool_calls": [], "reply": raw, "done": True}

    result = {
        "thought":    parsed.get("thought", ""),
        "tool_calls": parsed.get("tool_calls", []) or [],
        "reply":      parsed.get("reply", ""),
        "done":       bool(parsed.get("done", False)),
    }

    # L6 validation (hallucination + prompt leak + narration check)
    if result["reply"] and tool_results:
        from core_orch_layer6 import layer_6_validate
        validation = await layer_6_validate(
            ctx["intent"], result["reply"], tool_results
        )
        if not validation["ok"]:
            # Inject correction as new loop turn
            result["reply"]      = ""
            result["done"]       = False
            result["tool_calls"] = []
            result["thought"]    = f"[L6 correction] {validation.get('issues', [])}"

    return result


async def call_model_json(system: str, user: str,
                           max_tokens: int = MAX_TOKENS_JSON) -> dict:
    """
    Cheap structured call for pre-flight, critic, plan generation.
    Returns parsed dict or {} on failure.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    raw  = await loop.run_in_executor(
        None,
        lambda: _chain_call(system, user, max_tokens, json_mode=True)
    )
    try:
        return json.loads(_strip_json(raw))
    except Exception as e:
        print(f"[L8] call_model_json parse failed: {e} raw={raw[:100]!r}")
        return {}


async def call_model_simple(user: str, minimal_context: dict) -> str:
    """
    Plain text call for trivial fast-path.
    Returns string reply.
    """
    import asyncio
    system = minimal_context.get("system", "You are CORE. Answer concisely.")
    loop   = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _chain_call(system, user, MAX_TOKENS_SIMPLE)
    )


if __name__ == "__main__":
    print(f"🛰️ Layer 8: Coordination — Online. Primary model: {CLAUDE_MODEL}")
