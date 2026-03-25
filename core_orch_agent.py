"""
core_orch_agent.py — CORE AGI Agentic Loop (ReAct Pattern)
===========================================================
Activated for complex, open-ended, multi-step requests.
Replaces single-pass L4->L5 with a Think->Act->Observe loop.

DESIGNED FOR OPUS: model-agnostic via AGENT_MODEL env var.
- Today:  AGENT_MODEL="" uses Groq (fast, cheap, testing)
- Opus:   AGENT_MODEL=anthropic/claude-opus-4-5 (just change env var)
- Any:    AGENT_MODEL=any OpenRouter model string
"""

import asyncio
import json
import os
import time
from typing import Any, Dict, List

import httpx

from orchestrator_message import OrchestratorMessage

# ── Config ───────────────────────────────────────────────────────────────────
AGENT_MODEL          = os.getenv("AGENT_MODEL", "")             # empty = Groq
AGENT_MAX_STEPS      = int(os.getenv("AGENT_MAX_STEPS", "30"))
AGENT_TOKEN_BUDGET   = int(os.getenv("AGENT_TOKEN_BUDGET", "120000"))
AGENT_PROGRESS_EVERY = int(os.getenv("AGENT_PROGRESS_EVERY", "3"))
AGENT_ERROR_THRESHOLD = int(os.getenv("AGENT_ERROR_THRESHOLD", "4"))

# Phrases that trigger agentic mode in L4
AGENTIC_TRIGGERS = frozenset([
    "until", "keep trying", "keep going", "figure out", "research",
    "investigate", "build me", "create a full", "autonomously",
    "step by step", "comprehensive", "in depth", "thorough",
    "find all", "scan all", "check all", "iterate", "loop",
    "repeat until", "try until", "work until", "dont stop",
    "as many as", "exhaustive", "complete guide", "full analysis",
])

# ── System prompt ────────────────────────────────────────────────────────────
_AGENT_SYSTEM = (
    "You are CORE — an autonomous AGI system on an Oracle Cloud Ubuntu VM. "
    "You have 171+ tools: web_search, web_fetch, run_python, shell, file_list, "
    "file_read, file_write, search_kb, add_knowledge, calc, weather, get_time, "
    "get_state, get_system_health, task_add, notify_owner, sb_query, sb_insert, "
    "deploy_status, railway_logs_live, get_mistakes, list_evolutions, and more.\n\n"
    "You are in AGENTIC MODE — a ReAct loop: Think -> Act -> Observe -> repeat.\n\n"
    "RULES:\n"
    "1. Return EXACTLY one JSON object per iteration — no other text.\n"
    "2. Use one of these types:\n\n"
    "   ACTION: {\"type\": \"action\", \"thought\": \"why\", \"tool\": \"exact_tool_name\", \"args\": {}, \"progress\": \"brief status\"}\n"
    "   DONE:   {\"type\": \"done\", \"thought\": \"what was done\", \"answer\": \"full response to user\"}\n"
    "   STUCK:  {\"type\": \"stuck\", \"reason\": \"what blocks\", \"partial_answer\": \"what was found\"}\n\n"
    "3. Tool names must EXACTLY match the registry.\n"
    "4. Use thought to reason before acting.\n"
    "5. When DONE: answer must be the complete final response.\n"
    "6. Be efficient — do not repeat completed steps.\n"
    "Return ONLY valid JSON. No markdown, no preamble."
)

# ── Token estimation ─────────────────────────────────────────────────────────
def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ~ 4 chars."""
    return len(text) // 4


def _compress_history(history: List[Dict], keep_last: int = 5):
    """Compress old steps into summary when token budget runs low."""
    if len(history) <= keep_last:
        return "", history
    old = history[:-keep_last]
    recent = history[-keep_last:]
    parts = [f"[COMPRESSED: {len(old)} earlier steps]"]
    for step in old:
        if step.get("type") == "action":
            tool = step.get("tool", "?")
            ok = step.get("result", {}).get("ok", "?") if isinstance(step.get("result"), dict) else "?"
            parts.append(f"  - {tool}(ok={ok}): {step.get('summary', '')[:80]}")
    return "\n".join(parts), recent


# ── LLM think call ───────────────────────────────────────────────────────────
def _llm_think(system: str, prompt: str, max_tokens: int = 1024) -> str:
    """
    Agent LLM call using the full fallback chain from core_config.gemini_chat:
      1. OpenRouter → AGENT_MODEL (or OPENROUTER_MODEL if AGENT_MODEL not set)
         - Today: google/gemini-2.5-flash
         - Opus:  set AGENT_MODEL=anthropic/claude-opus-4-5
      2. Gemini direct API (round-robin all GEMINI_KEYS — up to 11 keys)
      3. Groq (strongest free model — final safety net)
    No custom HTTP code needed — gemini_chat handles everything.
    """
    from core_config import gemini_chat
    # Use AGENT_MODEL env var if set, otherwise gemini_chat uses OPENROUTER_MODEL
    return gemini_chat(
        system=system,
        user=prompt,
        max_tokens=max_tokens,
        model=AGENT_MODEL,  # "" = use OPENROUTER_MODEL default (gemini-2.5-flash)
    )



# ── Tool executor ─────────────────────────────────────────────────────────────
async def _run_tool(tool_name: str, args: Dict[str, Any], msg: OrchestratorMessage) -> Dict[str, Any]:
    """Execute a single tool by name with args."""
    try:
        from core_tools import TOOLS
        import inspect
        if tool_name not in TOOLS:
            return {"ok": False, "error": f"Tool '{tool_name}' not found ({len(TOOLS)} tools available)"}
        entry = TOOLS[tool_name]
        fn = entry.get("fn") if isinstance(entry, dict) else entry
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


# ── Progress notification ─────────────────────────────────────────────────────
async def _send_progress(msg: OrchestratorMessage, step: int, max_steps: int, progress: str) -> None:
    """Send brief Telegram update so owner sees CORE working in real-time."""
    try:
        from core_github import notify
        filled = int((step / max_steps) * 10)
        bar = "█" * filled + "░" * (10 - filled)
        text = f"⚙️ <b>CORE working...</b> [{bar}] step {step}/{max_steps}\n<code>{progress[:120]}</code>"
        notify(text, cid=str(msg.chat_id))
    except Exception as e:
        print(f"[AGENT] Progress notify failed: {e}")


# ── Compact tool list ─────────────────────────────────────────────────────────
_TOOLS_CACHE: str = ""

def _get_tools_summary() -> str:
    global _TOOLS_CACHE
    if _TOOLS_CACHE:
        return _TOOLS_CACHE
    try:
        from core_tools import TOOLS
        groups = {
            "TIME/STATE":  ["get_time", "datetime_now", "get_state", "get_system_health", "get_state_key"],
            "KNOWLEDGE":   ["search_kb", "add_knowledge", "kb_update", "get_mistakes", "log_mistake", "get_behavioral_rules"],
            "WEB":         ["web_search", "web_fetch", "summarize_url"],
            "CODE/VM":     ["run_python", "shell", "file_list", "file_read", "file_write", "run_script", "install_package"],
            "GITHUB":      ["read_file", "write_file", "gh_read_lines", "gh_search_replace", "multi_patch", "smart_patch"],
            "DATABASE":    ["sb_query", "sb_insert", "sb_patch", "sb_upsert", "sb_delete", "get_table_schema"],
            "TASKS/GOALS": ["get_state", "task_add", "task_update", "checkpoint", "get_active_goals", "set_goal"],
            "TRAINING":    ["list_evolutions", "approve_evolution", "trigger_cold_processor", "get_training_pipeline"],
            "DEPLOY":      ["deploy_status", "railway_logs_live", "redeploy", "build_status", "ping_health"],
            "UTILS":       ["calc", "weather", "currency", "translate", "generate_image", "list_tools"],
            "NOTIFY":      ["notify_owner"],
            "CRYPTO":      ["crypto_price", "crypto_balance", "crypto_trade"],
            "SELF-IMPROVE":["reason_chain", "decompose_task", "lookahead", "impact_model"],
        }
        lines = []
        covered = set()
        for cat, tools in groups.items():
            existing = [t for t in tools if t in TOOLS]
            if existing:
                lines.append(f"[{cat}] {chr(44).join(existing)}")
                covered.update(existing)
        misc = [t for t in TOOLS if t not in covered]
        if misc:
            lines.append(f"[OTHER({len(misc)})] {chr(44).join(sorted(misc)[:15])}...")
        _TOOLS_CACHE = "\n".join(lines)
    except Exception as e:
        _TOOLS_CACHE = f"(tool list error: {e})"
    return _TOOLS_CACHE


# ── Prompt builder ────────────────────────────────────────────────────────────
def _build_prompt(goal: str, history: List[Dict], compressed_summary: str, tools_summary: str) -> str:
    parts = [f"GOAL: {goal}", ""]
    if compressed_summary:
        parts.append(compressed_summary)
        parts.append("")
    if history:
        parts.append("STEP HISTORY:")
        for h in history:
            step_num = h.get("step", "?")
            if h.get("type") == "action":
                tool = h.get("tool", "?")
                thought = h.get("thought", "")[:80]
                result = h.get("result", {})
                if isinstance(result, dict):
                    ok = result.get("ok", "?")
                    err = result.get("error", "")
                    summary = h.get("summary", "")[:200]
                    if err:
                        parts.append(f"  [{step_num}] {tool}(ok={ok}) ERROR: {err[:120]}")
                    else:
                        parts.append(f"  [{step_num}] {tool}(ok={ok}) -> {summary}")
                else:
                    parts.append(f"  [{step_num}] {tool} -> {str(result)[:150]}")
            elif h.get("type") == "thought_only":
                parts.append(f"  [{step_num}] [error] {h.get('thought', '')[:80]}")
        parts.append("")
    parts.append(f"AVAILABLE TOOLS:")
    parts.append(tools_summary)
    parts.append("")
    parts.append("What is your next action? Return JSON only.")
    return "\n".join(parts)


# ── Main agentic loop ─────────────────────────────────────────────────────────
async def run_agent_loop(msg: OrchestratorMessage, goal: str) -> None:
    """
    Main ReAct loop.
    Runs until: goal reached | step budget | token budget | error threshold.
    """
    model_label = AGENT_MODEL or "groq"
    print(f"[AGENT] Start. goal={goal[:80]!r} max={AGENT_MAX_STEPS} model={model_label}")
    msg.track_layer("AGENT-START")

    history: List[Dict] = []
    tools_summary = _get_tools_summary()
    consecutive_errors = 0
    compressed_summary = ""
    start_time = time.monotonic()

    for step in range(1, AGENT_MAX_STEPS + 1):

        # Token budget check
        prompt = _build_prompt(goal, history, compressed_summary, tools_summary)
        tokens = _estimate_tokens(prompt)
        if tokens > AGENT_TOKEN_BUDGET * 0.8:
            print(f"[AGENT] Compressing history at ~{tokens} tokens")
            compressed_summary, history = _compress_history(history, keep_last=5)
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary)

        print(f"[AGENT] step={step} thinking (~{_estimate_tokens(prompt)} tokens)")

        # LLM think
        try:
            raw = _llm_think(_AGENT_SYSTEM, prompt, max_tokens=1024)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            decision = json.loads(raw)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[AGENT] step={step} parse error ({consecutive_errors}): {e}")
            if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                msg.styled_response = f"CORE agent aborted after {step} steps: too many errors. Last: {e}"
                break
            history.append({"type": "thought_only", "thought": f"Error: {e}", "step": step})
            continue

        dtype = decision.get("type", "")

        # DONE
        if dtype == "done":
            elapsed = round(time.monotonic() - start_time, 1)
            print(f"[AGENT] DONE at step={step} elapsed={elapsed}s")
            msg.styled_response = decision.get("answer", "Goal reached.")
            msg.track_layer(f"AGENT-DONE-step{step}")
            break

        # STUCK
        elif dtype == "stuck":
            reason = decision.get("reason", "Cannot proceed.")
            partial = decision.get("partial_answer", "")
            msg.styled_response = f"CORE is stuck: {reason}"
            if partial:
                msg.styled_response += f"\n\nPartial findings:\n{partial}"
            msg.track_layer(f"AGENT-STUCK-step{step}")
            break

        # ACTION
        elif dtype == "action":
            tool_name = decision.get("tool", "")
            tool_args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")
            progress = decision.get("progress", f"Running {tool_name}...")

            print(f"[AGENT] step={step} -> {tool_name}({json.dumps(tool_args, default=str)[:80]})")

            # Progress update every N steps
            if step % AGENT_PROGRESS_EVERY == 0:
                asyncio.ensure_future(_send_progress(msg, step, AGENT_MAX_STEPS, progress))

            # Execute
            result = await _run_tool(tool_name, tool_args, msg)
            ok = result.get("ok", False) if isinstance(result, dict) else True
            print(f"[AGENT] step={step} {tool_name} ok={ok}")

            if not ok:
                consecutive_errors += 1
                if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                    msg.styled_response = (
                        f"CORE agent aborted after {step} steps: "
                        f"{consecutive_errors} consecutive failures. Last: {tool_name}"
                    )
                    break
            else:
                consecutive_errors = 0

            # Summarise result for history
            if isinstance(result, dict):
                summary = (result.get("summary") or result.get("formatted")
                           or result.get("output", "")
                           or json.dumps({k: result[k] for k in list(result.keys())[:5]}, default=str)[:300])
            else:
                summary = str(result)[:300]

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

        else:
            consecutive_errors += 1
            print(f"[AGENT] step={step} unknown type: {dtype!r}")
            history.append({"type": "thought_only", "thought": f"Unknown: {dtype}", "step": step})

    else:
        # Step budget exhausted
        elapsed = round(time.monotonic() - start_time, 1)
        print(f"[AGENT] Budget exhausted ({AGENT_MAX_STEPS} steps, {elapsed}s)")
        try:
            summary_prompt = (
                f"GOAL: {goal}\n\n"
                f"You ran {len(history)} steps and hit the step limit ({elapsed}s). "
                f"Summarise findings and current state in a useful response."
            )
            summary = _llm_think(_AGENT_SYSTEM, summary_prompt, max_tokens=600)
            msg.styled_response = f"Step limit reached ({AGENT_MAX_STEPS} steps, {elapsed}s).\n\n{summary}"
        except Exception:
            msg.styled_response = f"Step limit reached after {AGENT_MAX_STEPS} steps. Partial work logged."

    msg.track_layer("AGENT-END")
    from core_orch_layer10 import layer_10_output
    await layer_10_output(msg)


# ── Trigger detection ─────────────────────────────────────────────────────────
def is_agentic_request(text: str, intent: str) -> bool:
    """Returns True if this request should use the agentic loop."""
    lower = text.lower()
    if any(t in lower for t in AGENTIC_TRIGGERS):
        return True
    if intent == "task_execution" and len(text) > 150:
        return True
    if lower.count(" then ") >= 2:
        return True
    return False
