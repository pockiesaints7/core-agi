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
    "RULES:\n"
    "1. Return EXACTLY one JSON object per iteration — no other text.\n"
    "2. Use one of these response types:\n\n"
    "   ACTION: {\"type\": \"action\", \"thought\": \"why\", \"tool\": \"exact_tool_name\", \"args\": {}, \"progress\": \"brief status\"}\n"
    "   DONE:   {\"type\": \"done\", \"thought\": \"what was accomplished\", \"answer\": \"full response to user\"}\n"
    "   STUCK:  {\"type\": \"stuck\", \"reason\": \"what blocks\", \"partial_answer\": \"what was found\"}\n\n"
    "3. Tool names must EXACTLY match the registry.\n"
    "4. Use \"thought\" to reason step-by-step before choosing action.\n"
    "5. When DONE: \"answer\" must be the complete, final, formatted response. No placeholders.\n"
    "6. Be efficient — never repeat a completed step. Use all gathered info before calling DONE.\n"
    "7. If a tool fails, try a different approach — do NOT retry the same call.\n"
    "Return ONLY valid JSON. No markdown, no preamble, no explanation outside the JSON."
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

# ── LLM think ────────────────────────────────────────────────────────────────
def _llm_think(system: str, prompt: str, max_tokens: int = 1500) -> str:
    """
    Full LLM fallback chain via core_config.gemini_chat:
      1. OpenRouter → AGENT_MODEL (gemini-2.5-flash by default, Opus when key set)
      2. Gemini direct API → round-robin all GEMINI_KEYS (up to 11)
      3. Groq → strongest free model (final safety net)
    """
    from core_config import gemini_chat
    return gemini_chat(
        system=system,
        user=prompt,
        max_tokens=max_tokens,
        model=AGENT_MODEL,  # "" = use OPENROUTER_MODEL env default
    )

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
        # Strip unknown kwargs gracefully
        sig = inspect.signature(fn)
        valid = set(sig.parameters.keys())
        filtered = {k: v for k, v in args.items() if k in valid}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: fn(**filtered))
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}
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

    # ── Cumulative ingestion budget ───────────────────────────────────────────
    # Tracks total chars of raw tool results processed this session.
    # Independent of prompt size — catches the case where compression keeps
    # resetting prompt budget but the model has still seen enormous data overall.
    # Per-model practical limits (tokens × 4 chars × 0.85 safety margin):
    _MODEL_CHAR_LIMITS = {
        "groq":   460_000,    # 128k tokens × 4 × 0.90
        "gemini": 900_000,    # 250k tokens × 4 × 0.90
        "opus":   680_000,    # 200k tokens × 4 × 0.85
    }
    _model_key = "opus" if "opus" in model_label else ("gemini" if "gemini" in model_label else "groq")
    total_chars_ingested = 0
    cumulative_limit = _MODEL_CHAR_LIMITS[_model_key]
    print(f"[AGENT] Budgets: prompt={AGENT_TOKEN_BUDGET//4}t cumulative={cumulative_limit//4}t timeout={AGENT_TIMEOUT_SEC}s")

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

        if not force_conclude and char_count > AGENT_TOKEN_BUDGET * AGENT_TOKEN_COMPRESS:
            # Compress old history to free up budget
            print(f"[AGENT] step={step} compressing history ({char_count:,} chars)")
            compressed_summary, history = _compress_history(history, keep_last=6)
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=False)
            char_count = _chars(prompt)

        if not force_conclude and char_count > AGENT_TOKEN_BUDGET * AGENT_TOKEN_CONCLUDE:
            # Budget critical — tell LLM to conclude now
            print(f"[AGENT] step={step} budget critical ({char_count:,} chars) — forcing conclusion")
            force_conclude = True
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=True)

        print(f"[AGENT] step={step} thinking (~{char_count//4} tokens) elapsed={elapsed:.1f}s")

        # ── LLM think ──────────────────────────────────────────────────────────
        try:
            raw = _llm_think(_AGENT_SYSTEM, prompt, max_tokens=1500)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            decision = json.loads(raw)
            consecutive_errors = 0
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

            # Repeat guard: same tool + same args failed last step → skip, inject note
            if (history
                    and history[-1].get("type") == "action"
                    and history[-1].get("tool") == tool_name
                    and history[-1].get("args") == tool_args
                    and not history[-1].get("result", {}).get("ok", True)):
                note = f"SKIPPED: {tool_name} already failed with identical args. Use a different tool or different args."
                print(f"[AGENT] step={step} repeat-skip {tool_name}")
                history.append({"type": "thought_only", "thought": note, "step": step})
                consecutive_errors += 1
                if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                    msg.styled_response = f"⚠️ CORE stuck in loop on {tool_name} — aborting."
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

            # ── Cumulative ingestion tracking ─────────────────────────────────
            result_chars = len(json.dumps(result, default=str))
            total_chars_ingested += result_chars
            if total_chars_ingested > cumulative_limit:
                print(f"[AGENT] step={step} cumulative ingestion limit reached "
                      f"({total_chars_ingested:,}/{cumulative_limit:,} chars) — forcing conclusion")
                force_conclude = True

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
