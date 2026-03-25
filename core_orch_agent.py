"""
core_orch_agent.py — CORE AGI Agentic Loop (ReAct Pattern)
===========================================================
NO artificial step limit. Runs until a real termination condition:
  1. LLM returns type=done       — goal reached, full answer ready
  2. LLM returns type=stuck      — cannot proceed, returns partial
  3. Token budget exhausted      — compresses history, forces conclusion
  4. Wall-clock timeout          — configurable, prevents runaway jobs
  5. N consecutive errors        — something is broken, abort cleanly

DESIGNED FOR OPUS: model-agnostic via AGENT_MODEL env var.
- Today:  AGENT_MODEL="" -> OPENROUTER_MODEL (google/gemini-2.5-flash)
- Opus:   export AGENT_MODEL=anthropic/claude-opus-4-5 -> done, no code change
- Any:    AGENT_MODEL=any OpenRouter model string
"""

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from orchestrator_message import OrchestratorMessage

# ── Config ────────────────────────────────────────────────────────────────────
AGENT_MODEL           = os.getenv("AGENT_MODEL", "")         # empty = OPENROUTER_MODEL
AGENT_TOKEN_BUDGET    = int(os.getenv("AGENT_TOKEN_BUDGET", "140000"))   # chars/4 ≈ tokens
AGENT_TOKEN_COMPRESS  = float(os.getenv("AGENT_TOKEN_COMPRESS", "0.75")) # compress at 75% budget
AGENT_TOKEN_CONCLUDE  = float(os.getenv("AGENT_TOKEN_CONCLUDE", "0.92")) # force conclusion at 92%
AGENT_TIMEOUT_SEC     = int(os.getenv("AGENT_TIMEOUT_SEC", "600"))       # 10min wall clock
AGENT_ERROR_THRESHOLD = int(os.getenv("AGENT_ERROR_THRESHOLD", "4"))     # consecutive failures
AGENT_PROGRESS_EVERY  = int(os.getenv("AGENT_PROGRESS_EVERY", "3"))      # telegram update interval

# ── Agentic trigger keywords ──────────────────────────────────────────────────
AGENTIC_TRIGGERS = frozenset([
    "until", "keep trying", "keep going", "figure out", "research",
    "investigate", "build me", "create a full", "autonomously",
    "step by step", "comprehensive", "in depth", "thorough",
    "find all", "scan all", "check all", "iterate", "loop",
    "repeat until", "try until", "work until", "dont stop",
    "as many as", "exhaustive", "complete guide", "full analysis",
])

# ── System prompt ─────────────────────────────────────────────────────────────
_AGENT_SYSTEM = (
    "You are CORE — an autonomous AGI system on an Oracle Cloud Ubuntu VM. "
    "You have 171+ tools: web_search, web_fetch, run_python, shell, file_list, "
    "file_read, file_write, search_kb, add_knowledge, calc, weather, get_time, "
    "get_state, get_system_health, task_add, notify_owner, sb_query, sb_insert, "
    "deploy_status, railway_logs_live, get_mistakes, list_evolutions, and more.\n\n"
    "You are in AGENTIC MODE — a ReAct loop: Think -> Act -> Observe -> repeat.\n\n"
    "CRITICAL RULES:\n"
    "1. Return EXACTLY one JSON object per iteration — no other text.\n"
    "2. Use one of these response types:\n\n"
    "   ACTION: {\"type\": \"action\", \"thought\": \"why\", \"tool\": \"exact_tool_name\", \"args\": {}, \"progress\": \"brief status\"}\n"
    "   DONE:   {\"type\": \"done\", \"thought\": \"what was accomplished\", \"answer\": \"full response to user\"}\n"
    "   STUCK:  {\"type\": \"stuck\", \"reason\": \"what blocks\", \"partial_answer\": \"what was found\"}\n\n"
    "3. Tool names must EXACTLY match the registry.\n"
    "4. EFFICIENCY — before each action ask: do I already have this data from a previous step? If YES → do NOT call the tool again, use what you have.\n"
    "5. CONVERGENCE — once you have gathered enough data to answer the goal, return type=done IMMEDIATELY. Do not keep collecting more data.\n"
    "6. DONE threshold: if you have attempted every distinct subtask in the goal at least once, return type=done with a full synthesis of everything collected.\n"
    "7. If a tool fails once, try ONE alternative approach. If that also fails, skip it and note it in the answer.\n"
    "\nSUPABASE TOOL USAGE (exact param names):\n"
    "  sb_query(table, filters='col=eq.val', order='col.desc', select='col1,col2', limit=10)\n"
    "  sb_insert(table, data={'col': 'val'})\n"
    "  sb_patch(table, filters='id=eq.X', data={'col': 'newval'})\n"
    "  sb_upsert(table, data={'col': 'val'}, on_conflict='col')\n"
    "  add_knowledge(domain, topic, content, confidence='high')\n"
    "  log_mistake(domain, what_failed, correct_approach, severity='low')\n"
    "  Known tables: knowledge_base, task_queue, mistakes, sessions, behavioral_rules, evolution_queue\n"
    "  filters use PostgREST syntax: 'status=eq.pending', 'id=gt.1', 'domain=eq.code'\n"
    "  order uses: 'created_at.desc' or 'id.asc' (NOT order_by or order_direction)\n"
    "8. When DONE: \"answer\" must be the complete formatted response. No placeholders. Synthesise ALL gathered data.\n"
    "Return ONLY valid JSON. No markdown, no preamble."
)

# Injected when token budget is running low — tells LLM to wrap up
_CONCLUDE_INSTRUCTION = (
    "\n\nIMPORTANT: Context budget is nearly full. "
    "You MUST return type=done on this step with a comprehensive answer using everything collected so far. "
    "Do not call any more tools. Synthesise all gathered information into the final answer now."
)

# ── Token utilities ───────────────────────────────────────────────────────────
def _chars(text: str) -> int:
    """Estimate token usage as char count (4 chars ≈ 1 token)."""
    return len(text)

def _compress_history(history: List[Dict], keep_last: int = 6) -> Tuple[str, List[Dict]]:
    """
    Compress old steps into a summary string.
    Keeps the most recent `keep_last` steps verbatim for full context.
    Returns (summary_text, recent_history).
    """
    if len(history) <= keep_last:
        return "", history
    old = history[:-keep_last]
    recent = history[-keep_last:]
    parts = [f"[COMPRESSED HISTORY — {len(old)} earlier steps, key findings below]"]
    for h in old:
        if h.get("type") == "action":
            tool = h.get("tool", "?")
            ok = h.get("result", {}).get("ok", True) if isinstance(h.get("result"), dict) else True
            summary = h.get("summary", "")[:120]
            status = "✓" if ok else "✗"
            parts.append(f"  {status} {tool}: {summary}")
    return "\n".join(parts), recent

# ── Context limit sentinel ───────────────────────────────────────────────────
class ContextLimitError(Exception):
    """Raised when the API explicitly reports context length exceeded."""
    pass

# ── LLM think ────────────────────────────────────────────────────────────────
def _llm_think(system: str, prompt: str, max_tokens: int = 1500):
    """
    Returns (response_text, prompt_tokens_actually_used).

    prompt_tokens comes from the REAL API response — not estimated, not hardcoded.
    This is the only correct way to track context: ask the API how much was used.

    Chain: OpenRouter → Gemini direct (11 keys) → Groq
    If context limit exceeded → raises ContextLimitError (caller forces conclusion).
    """
    import httpx as _httpx
    import time as _time
    from core_config import OPENROUTER_API_KEY

    active_model = AGENT_MODEL or os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    # ── Tier 1: OpenRouter ────────────────────────────────────────────────────
    if OPENROUTER_API_KEY:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        last_err = None
        for attempt in range(3):
            try:
                t0 = _time.monotonic()
                r = _httpx.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": f"https://{os.getenv('PUBLIC_DOMAIN', 'core-agi.duckdns.org')}",
                        "X-Title": "CORE AGI Agent",
                    },
                    json={
                        "model": active_model,
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                        "messages": messages,
                    },
                    timeout=90,
                )
                elapsed = round(_time.monotonic() - t0, 2)
                if elapsed > 10:
                    print(f"[AGENT] LLM slow: {elapsed}s model={active_model}")
                if r.status_code == 429:
                    _time.sleep(3 * (attempt + 1))
                    continue
                # Context length exceeded — API tells us explicitly
                if r.status_code in (400, 413):
                    err_body = r.json().get("error", {})
                    err_str = str(err_body).lower()
                    if any(k in err_str for k in ("context", "length", "too long", "token")):
                        raise ContextLimitError(f"Context limit hit on {active_model}: {err_body}")
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"].strip()
                # ← Real token count from API. No guessing. No hardcoded limits.
                prompt_tokens = data.get("usage", {}).get("prompt_tokens", len(prompt) // 4)
                return text, prompt_tokens
            except ContextLimitError:
                raise  # propagate immediately to caller
            except Exception as e:
                last_err = e
                continue
        print(f"[AGENT] OpenRouter failed ({last_err}) — trying Gemini direct")

    # ── Tier 2: Gemini direct — real promptTokenCount from usageMetadata ────────
    from core_config import _GEMINI_KEYS, _GEMINI_MODEL
    import core_config as _cc
    if _GEMINI_KEYS:
        try:
            import httpx as _hx
            # Round-robin key selection via module-level index
            key = _GEMINI_KEYS[_cc._GEMINI_KEY_INDEX % len(_GEMINI_KEYS)]
            _cc._GEMINI_KEY_INDEX = (_cc._GEMINI_KEY_INDEX + 1) % len(_GEMINI_KEYS)
            contents = []
            if system:
                # Gemini system turn pattern
                contents.append({"role": "user",  "parts": [{"text": system}]})
                contents.append({"role": "model", "parts": [{"text": "Understood."}]})
            contents.append({"role": "user", "parts": [{"text": prompt}]})
            r = _hx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent",
                params={"key": key},
                headers={"Content-Type": "application/json"},
                json={
                    "contents": contents,
                    "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
                    "safetySettings": [
                        {"category": "HARM_CATEGORY_HARASSMENT",       "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH",      "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT","threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT","threshold": "BLOCK_NONE"},
                    ],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Real token count — Gemini returns it in usageMetadata.promptTokenCount
            prompt_tokens = data.get("usageMetadata", {}).get("promptTokenCount", len(prompt) // 4)
            return text, prompt_tokens
        except Exception as e:
            print(f"[AGENT] Gemini direct failed ({e}) — trying Groq")

    # ── Tier 3: Groq — also returns real prompt_tokens (same OpenAI format) ───
    from core_config import GROQ_MODEL
    import httpx as _hx2
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            r = _hx2.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "max_tokens": max_tokens, "temperature": 0.1, "messages": messages},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip()
            # Groq also returns prompt_tokens in usage (OpenAI-compatible format)
            prompt_tokens = data.get("usage", {}).get("prompt_tokens", len(prompt) // 4)
            return text, prompt_tokens
        except Exception as e:
            raise RuntimeError(f"All LLM tiers failed. Groq: {e}")

# ── Tool executor ─────────────────────────────────────────────────────────────
async def _run_tool(tool_name: str, args: Dict[str, Any], msg: OrchestratorMessage) -> Dict[str, Any]:
    """Execute a tool by name. Returns result dict. Missing ok key = success."""
    try:
        from core_tools import TOOLS
        import inspect
        if tool_name not in TOOLS:
            return {"ok": False, "error": f"Unknown tool: {tool_name!r}. Check spelling."}
        entry = TOOLS[tool_name]
        fn = entry.get("fn") if isinstance(entry, dict) else entry
        # Strip unknown kwargs gracefully — but warn LLM if args were silently dropped
        sig = inspect.signature(fn)
        valid = set(sig.parameters.keys())
        filtered = {k: v for k, v in args.items() if k in valid}
        stripped = [k for k in args if k not in valid]
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: fn(**filtered))
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        # Inject warning about stripped args so LLM knows to use correct param names
        if stripped:
            result = dict(result)
            result["ignored_args"] = stripped
            result["param_hint"] = f"Valid params for {tool_name}: {sorted(valid - {'self'})}"
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Progress ping ─────────────────────────────────────────────────────────────
async def _send_progress(msg: OrchestratorMessage, step: int, elapsed: float, progress: str) -> None:
    """Send Telegram progress update. Shows elapsed time, not artificial progress bar."""
    try:
        from core_github import notify
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
        text = (
            f"⚙️ <b>CORE working...</b> step {step} ({time_str} elapsed)\n"
            f"<code>{progress[:120]}</code>"
        )
        notify(text, cid=str(msg.chat_id))
    except Exception as e:
        print(f"[AGENT] Progress notify failed: {e}")

# ── Tool summary (cached) ─────────────────────────────────────────────────────
_TOOLS_CACHE: str = ""

def _get_tools_summary() -> str:
    global _TOOLS_CACHE
    if _TOOLS_CACHE:
        return _TOOLS_CACHE
    try:
        from core_tools import TOOLS
        groups = {
            "TIME/STATE":   ["get_time", "datetime_now", "get_state", "get_system_health", "get_state_key"],
            "KNOWLEDGE":    ["search_kb", "add_knowledge", "kb_update", "get_mistakes", "log_mistake", "get_behavioral_rules"],
            "WEB":          ["web_search", "web_fetch", "summarize_url"],
            "CODE/VM":      ["run_python", "shell", "file_list", "file_read", "file_write", "run_script", "install_package"],
            "GITHUB":       ["read_file", "write_file", "gh_read_lines", "gh_search_replace", "multi_patch", "smart_patch"],
            "DATABASE":     ["sb_query", "sb_insert", "sb_patch", "sb_upsert", "sb_delete", "get_table_schema"],
            "TASKS/GOALS":  ["task_add", "task_update", "checkpoint", "get_active_goals", "set_goal", "update_goal_progress"],
            "TRAINING":     ["list_evolutions", "approve_evolution", "trigger_cold_processor", "get_training_pipeline"],
            "DEPLOY":       ["deploy_status", "railway_logs_live", "redeploy", "build_status", "ping_health"],
            "UTILS":        ["calc", "weather", "currency", "translate", "generate_image", "list_tools"],
            "NOTIFY":       ["notify_owner"],
            "CRYPTO":       ["crypto_price", "crypto_balance", "crypto_trade"],
            "SELF-IMPROVE": ["reason_chain", "decompose_task", "lookahead", "impact_model"],
        }
        lines = []
        covered = set()
        for cat, tools in groups.items():
            existing = [t for t in tools if t in TOOLS]
            if existing:
                lines.append(f"[{cat}] {', '.join(existing)}")
                covered.update(existing)
        misc = sorted(t for t in TOOLS if t not in covered)
        if misc:
            lines.append(f"[OTHER({len(misc)})] {', '.join(misc[:20])}...")
        _TOOLS_CACHE = "\n".join(lines)
    except Exception as e:
        _TOOLS_CACHE = f"(tool list error: {e})"
    return _TOOLS_CACHE

# ── Prompt builder ────────────────────────────────────────────────────────────
def _build_prompt(
    goal: str,
    history: List[Dict],
    compressed_summary: str,
    tools_summary: str,
    conclude: bool = False,
) -> str:
    parts = [f"GOAL: {goal}", ""]
    if compressed_summary:
        parts.append(compressed_summary)
        parts.append("")
    if history:
        parts.append("STEP HISTORY (most recent last):")
        for h in history:
            step_num = h.get("step", "?")
            if h.get("type") == "action":
                tool = h.get("tool", "?")
                result = h.get("result", {})
                summary = h.get("summary", "")[:200]
                if isinstance(result, dict):
                    ok = result.get("ok", True)
                    err = result.get("error", "")
                    if not ok and err:
                        parts.append(f"  [{step_num}] ✗ {tool}: ERROR: {err[:150]}")
                    else:
                        parts.append(f"  [{step_num}] ✓ {tool}: {summary}")
                else:
                    parts.append(f"  [{step_num}] ✓ {tool}: {str(result)[:150]}")
            elif h.get("type") == "thought_only":
                parts.append(f"  [{step_num}] [note] {h.get('thought', '')[:100]}")
        parts.append("")
    parts.append("AVAILABLE TOOLS:")
    parts.append(tools_summary)
    parts.append("")
    if conclude:
        parts.append(_CONCLUDE_INSTRUCTION)
    else:
        parts.append("What is your next action? Return JSON only.")
    return "\n".join(parts)

# ── Main loop ─────────────────────────────────────────────────────────────────
async def run_agent_loop(msg: OrchestratorMessage, goal: str) -> None:
    """
    ReAct agentic loop. No artificial step limit.

    Terminates only when:
      1. LLM returns type=done         (goal achieved)
      2. LLM returns type=stuck        (cannot proceed)
      3. Token budget nearly full      (forces conclusion via _CONCLUDE_INSTRUCTION)
      4. Wall-clock timeout            (AGENT_TIMEOUT_SEC, default 10min)
      5. N consecutive tool errors     (AGENT_ERROR_THRESHOLD, default 4)
    """
    model_label = AGENT_MODEL or os.getenv("OPENROUTER_MODEL", "gemini-2.5-flash")
    print(f"[AGENT] Start. goal={goal[:80]!r} model={model_label} timeout={AGENT_TIMEOUT_SEC}s")
    msg.track_layer("AGENT-START")

    history: List[Dict] = []
    tools_summary = _get_tools_summary()
    consecutive_errors = 0
    compressed_summary = ""
    start_time = time.monotonic()
    step = 0
    force_conclude = False  # set True when any budget is critical

    # Token tracking: use real prompt_tokens from API response each step.
    # No hardcoded model limits — the API tells us the truth.
    last_prompt_tokens = 0   # updated every step from API response
    print(f"[AGENT] Budgets: prompt_compress={int(AGENT_TOKEN_COMPRESS*100)}% prompt_conclude={int(AGENT_TOKEN_CONCLUDE*100)}% timeout={AGENT_TIMEOUT_SEC}s")

    while True:
        step += 1
        elapsed = time.monotonic() - start_time

        # ── Termination: wall-clock timeout ───────────────────────────────────
        if elapsed > AGENT_TIMEOUT_SEC:
            print(f"[AGENT] Timeout after {elapsed:.0f}s at step {step}")
            force_conclude = True  # fall through to conclusion

        # ── Build prompt + check token budget ─────────────────────────────────
        prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=force_conclude)
        char_count = _chars(prompt)

        # Use real token count from last API call if available, else estimate from chars
        effective_tokens = last_prompt_tokens if last_prompt_tokens > 0 else char_count // 4
        compress_threshold = int(AGENT_TOKEN_BUDGET * AGENT_TOKEN_COMPRESS // 4)  # convert budget to tokens
        conclude_threshold = int(AGENT_TOKEN_BUDGET * AGENT_TOKEN_CONCLUDE // 4)

        if not force_conclude and effective_tokens > compress_threshold:
            print(f"[AGENT] step={step} compressing history (tokens={effective_tokens} > {compress_threshold})")
            compressed_summary, history = _compress_history(history, keep_last=6)
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=False)
            char_count = _chars(prompt)

        if not force_conclude and effective_tokens > conclude_threshold:
            print(f"[AGENT] step={step} token budget critical (tokens={effective_tokens} > {conclude_threshold}) — forcing conclusion")
            force_conclude = True
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=True)

        print(f"[AGENT] step={step} prompt_real={last_prompt_tokens}t prompt_est≈{char_count//4}t elapsed={elapsed:.1f}s")

        # ── LLM think ──────────────────────────────────────────────────────────
        try:
            raw, last_prompt_tokens = _llm_think(_AGENT_SYSTEM, prompt, max_tokens=1500)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            decision = json.loads(raw)
            consecutive_errors = 0
        except ContextLimitError as e:
            # API explicitly told us context is exceeded — force conclusion immediately
            print(f"[AGENT] step={step} CONTEXT LIMIT from API: {e}")
            force_conclude = True
            history.append({"type": "thought_only", "thought": f"Context limit hit: {e}", "step": step})
            continue
        except Exception as e:
            consecutive_errors += 1
            print(f"[AGENT] step={step} LLM/parse error ({consecutive_errors}/{AGENT_ERROR_THRESHOLD}): {e}")
            if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                msg.styled_response = (
                    f"⚠️ CORE agent halted after {step} steps ({elapsed:.0f}s): "
                    f"repeated LLM errors. Last: {e}"
                )
                break
            history.append({"type": "thought_only", "thought": f"LLM error: {e}", "step": step})
            continue

        dtype = decision.get("type", "")

        # ── DONE ──────────────────────────────────────────────────────────────
        if dtype == "done":
            print(f"[AGENT] DONE at step={step} elapsed={elapsed:.1f}s")
            msg.styled_response = decision.get("answer", "Goal reached.")
            msg.track_layer(f"AGENT-DONE-step{step}")
            break

        # ── STUCK ─────────────────────────────────────────────────────────────
        elif dtype == "stuck":
            reason = decision.get("reason", "Cannot proceed.")
            partial = decision.get("partial_answer", "")
            print(f"[AGENT] STUCK at step={step}: {reason[:80]}")
            msg.styled_response = f"⚠️ CORE stuck: {reason}"
            if partial:
                msg.styled_response += f"\n\nPartial findings:\n{partial}"
            msg.track_layer(f"AGENT-STUCK-step{step}")
            break

        # ── ACTION ────────────────────────────────────────────────────────────
        elif dtype == "action":
            tool_name = decision.get("tool", "")
            tool_args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")
            progress = decision.get("progress", f"Running {tool_name}...")

            print(f"[AGENT] step={step} → {tool_name}({json.dumps(tool_args, default=str)[:80]})")

            # Progress update every N steps
            if step % AGENT_PROGRESS_EVERY == 0:
                asyncio.ensure_future(_send_progress(msg, step, elapsed, progress))

            # Repeat guard: same tool + same args called recently → skip
            # Catches both: repeated failures AND repeated successful calls (infinite loop)
            recent_calls = [(h.get("tool"), json.dumps(h.get("args",{}), sort_keys=True))
                            for h in history[-5:] if h.get("type") == "action"]
            this_call = (tool_name, json.dumps(tool_args, sort_keys=True))
            repeat_count = recent_calls.count(this_call)
            if repeat_count >= 2:
                note = (f"STOP REPEATING: {tool_name} with these args was already called "
                        f"{repeat_count} times recently. You have that data. "
                        f"Move on to the next goal or return type=done with what you have.")
                print(f"[AGENT] step={step} repeat-guard {tool_name} (×{repeat_count} in last 5 steps)")
                history.append({"type": "thought_only", "thought": note, "step": step})
                consecutive_errors += 1
                if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                    msg.styled_response = f"⚠️ CORE stuck looping on {tool_name} — aborting."
                    break
                continue

            # Execute tool
            result = await _run_tool(tool_name, tool_args, msg)
            ok = result.get("ok", True) if isinstance(result, dict) else True
            print(f"[AGENT] step={step} {tool_name} ok={ok}")

            if not ok:
                consecutive_errors += 1
                if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                    msg.styled_response = (
                        f"⚠️ CORE agent halted: {consecutive_errors} consecutive tool failures "
                        f"at step {step}. Last failed: {tool_name}"
                    )
                    break
            else:
                consecutive_errors = 0

            # Compact result summary for history
            if isinstance(result, dict):
                summary = (
                    result.get("summary") or result.get("formatted") or result.get("output", "")
                    or json.dumps({k: result[k] for k in list(result.keys())[:5]}, default=str)[:400]
                )
            else:
                summary = str(result)[:400]

            history.append({
                "type":    "action",
                "step":    step,
                "tool":    tool_name,
                "args":    tool_args,
                "thought": thought,
                "result":  result,
                "summary": summary,
            })
            msg.add_tool_result(tool_name, ok, result)

            # Token budget: checked at top of loop via last_prompt_tokens (real API value)
            # No additional tracking needed here — the API response tells us truth each step

        # ── UNKNOWN ───────────────────────────────────────────────────────────
        else:
            consecutive_errors += 1
            print(f"[AGENT] step={step} unknown type: {dtype!r}")
            history.append({"type": "thought_only", "thought": f"Unknown response type: {dtype!r}", "step": step})
            if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                msg.styled_response = f"⚠️ CORE agent halted: repeated invalid responses."
                break

    # ── Deliver via L10 ───────────────────────────────────────────────────────
    msg.track_layer("AGENT-END")
    elapsed_total = time.monotonic() - start_time
    print(f"[AGENT] End. steps={step} elapsed={elapsed_total:.1f}s response_len={len(msg.styled_response or '')}")
    from core_orch_layer10 import layer_10_output
    await layer_10_output(msg)


# ── Trigger detection ─────────────────────────────────────────────────────────
def is_agentic_request(text: str, intent: str) -> bool:
    """Returns True if this request should activate the agentic loop."""
    lower = text.lower()
    if any(t in lower for t in AGENTIC_TRIGGERS):
        return True
    if intent == "task_execution" and len(text) > 150:
        return True
    if lower.count(" then ") >= 2:
        return True
    return False
