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
AGENT_TOKEN_COMPRESS  = float(os.getenv("AGENT_TOKEN_COMPRESS", "0.50")) # compress at 50% budget (was 75% — too late)
AGENT_TOKEN_CONCLUDE  = float(os.getenv("AGENT_TOKEN_CONCLUDE", "0.92")) # force conclusion at 92%
AGENT_TIMEOUT_SEC     = int(os.getenv("AGENT_TIMEOUT_SEC", "600"))       # 10min wall clock
AGENT_ERROR_THRESHOLD = int(os.getenv("AGENT_ERROR_THRESHOLD", "4"))     # consecutive failures
AGENT_PROGRESS_EVERY  = int(os.getenv("AGENT_PROGRESS_EVERY", "3"))      # telegram update interval
AGENT_DISCOVERY_STEP_LIMIT = int(os.getenv("AGENT_DISCOVERY_STEP_LIMIT", "6"))  # discovery budget

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
    "You have 171+ tools: web_search, web_fetch, ingest_knowledge, run_python, shell, file_list, "
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
    "8. EVIDENCE GATE — before answering, prefer the strongest evidence available in this order: Supabase memory, current state, repo-map/code tools, direct local repo/code reads, public-source research via ingest_knowledge and related public fetchers, then web search/fetch. Combine sources when that makes the evidence stronger. Do not guess when evidence is sparse. If you cannot ground the answer after retrieval, return type=stuck or ask for the missing file/path/URL/commit.\n"
    "\nSUPABASE TOOL USAGE (exact param names):\n"
    "  sb_query(table, filters='col=eq.val', order='col.desc', select='col1,col2', limit=10)\n"
    "  sb_insert(table, data={'col': 'val'})\n"
    "  sb_patch(table, filters='id=eq.X', data={'col': 'newval'})\n"
    "  sb_upsert(table, data={'col': 'val'}, on_conflict='col')\n"
    "  add_knowledge(domain, topic, content, confidence='high')\n"
    "  log_mistake(domain, what_failed, correct_approach, severity='low')\n"
    "  Known tables: knowledge_base, task_queue, mistakes, sessions, behavioral_rules, evolution_queue, repo_components, repo_component_chunks, repo_component_edges\n"
    "  filters use PostgREST syntax: 'status=eq.pending', 'id=gt.1', 'domain=eq.code'\n"
    "  CRITICAL: multiple filters MUST use & separator: 'domain=eq.test&topic=eq.foo' NOT comma.\n"
    "  NEVER use comma in filters: 'domain=eq.test,topic=eq.foo' is WRONG and returns 0 rows.\n"
    "  order uses: 'created_at.desc' or 'id.asc' (NOT order_by or order_direction)\n"
    "8. When DONE: \"answer\" field rules:\n"
    "   - Include ACTUAL DATA from every tool result: real IDs, real values, real row contents, real error messages.\n"
    "   - DO NOT summarize with vague phrases like 'successfully retrieved 3 entries' — show the actual entries.\n"
    "   - For each step: state what was done, show key data returned (IDs, values, fields), state pass/fail.\n"
    "   - For errors: show the exact error message returned, not just 'an error occurred'.\n"
    "   - Format as a numbered list, one section per step. Be specific and data-rich.\n"
    "   - Synthesise ALL gathered data — include every step result with its actual output.\n"
    "9. For list_evolutions results: ALWAYS report total_count (real DB count) first, then show items. Never truncate the count.\n"
    "   Example: 'There are 666 pending evolutions. Showing newest 20: ...'. Never say '8 pending' if total_count says 666.\n"
    "10. PERSISTENT STATE — use agent_state_set/agent_state_get to remember data across steps:\n"
    "    - After inserting/creating anything: agent_state_set(key='kb_id', value='<id>')\n"
    "    - Before re-querying: check PERSISTENT STATE section above — if the data is there, use it directly.\n"
    "    - NEVER re-search for data you already have in PERSISTENT STATE.\n"
    "    - For multi-step tasks: call agent_session_init at start, agent_step_done after each step.\n"
    "    - agent_step_done: use a UNIQUE step_name per step ('step_1_query_tasks', 'step_2_kb_query', etc). NEVER reuse the same step_name.\n"
    "    - Call agent_step_done ONCE per step then immediately proceed to the NEXT step.\n"
    "11. STEP SEQUENCING for multi-step tasks:\n"
    "    - Execute steps IN ORDER: 1,2,3...10. Do not skip ahead or repeat a step.\n"
    "    - After completing ALL steps, return type=done with the full report.\n"
    "    - If you see already_done=True from agent_step_done, that step is complete — move to NEXT step.\n"
    "Return ONLY valid JSON. No markdown, no preamble."
)

# Injected when token budget is running low — tells LLM to wrap up
_CONCLUDE_INSTRUCTION = (
    "\n\nIMPORTANT: Context budget is nearly full. "
    "You MUST return type=done on this step with a comprehensive answer using everything collected so far. "
    "Do not call any more tools. Synthesise all gathered information into the final answer now."
)

_DISCOVERY_TOOLS = frozenset({
    "file_list",
    "shell",
    "repo_map_status",
    "repo_component_packet",
    "repo_graph_packet",
    "search_kb",
    "web_search",
    "ingest_knowledge",
})

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
            "TIME/STATE":   ["get_time", "datetime_now", "get_state", "get_system_health", "get_state_key", "session_snapshot", "state_packet", "state_consistency_check"],
            "KNOWLEDGE":    ["search_kb", "add_knowledge", "kb_update", "get_mistakes", "log_mistake", "get_behavioral_rules"],
            "WEB":          ["web_search", "web_fetch", "summarize_url"],
            "CODE/VM":      ["run_python", "shell", "file_list", "file_read", "file_write", "run_script", "install_package", "code_read_packet"],
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
    agent_state: dict = None,
) -> str:
    parts = [f"GOAL: {goal}", ""]
    # Inject persistent state scratchpad — survives across iterations
    import json as _j
    effective_state = agent_state or {}
    state_str = _j.dumps(effective_state, default=str)
    parts.append(f"PERSISTENT STATE (use these values — do NOT re-query for data already here):")
    parts.append(state_str)
    gate = effective_state.get("evidence_gate") or {}
    parts.append("EVIDENCE GATE (follow this before guessing):")
    parts.append(_j.dumps(gate, default=str))
    parts.append("If the gate says evidence is sparse, search Supabase first, then the repo map/local code state, then public-source research, then web. If still sparse, stop and ask for clarification or upload.")
    sid = effective_state.get("session_id", "default")
    parts.append(f"YOUR SESSION_ID IS: {sid!r} — use this exact value for all agent_* tool calls.")
    parts.append("")
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
    msg.agentic_metadata = {
        "goal": goal[:400],
        "model": model_label,
        "started_at": time.monotonic(),
        "request_kind": getattr(msg, "request_kind", ""),
        "response_mode": getattr(msg, "response_mode", ""),
        "agentic": True,
    }
    msg.context["agentic_metadata"] = msg.agentic_metadata
    if msg.context.get("evidence_gate"):
        msg.context["evidence_gate"] = msg.context.get("evidence_gate", {})
    msg.response_mode = "agentic"
    msg.context["response_mode"] = "agentic"
    msg.decision_packet = dict(msg.decision_packet or {})
    msg.decision_packet.update({
        "response_mode": "agentic",
        "agentic": True,
        "route_reason": "agentic_loop",
    })
    msg.context["decision_packet"] = msg.decision_packet
    msg.context["agentic_metadata"] = msg.agentic_metadata

    history: List[Dict] = []
    tools_summary = _get_tools_summary()
    consecutive_errors = 0
    compressed_summary = ""
    start_time = time.monotonic()
    step = 0
    force_conclude = False  # set True when any budget is critical
    discovery_steps = 0
    verification_goal = any(k in goal.lower() for k in ("git", "commit", "clean", "synced", "status", "verify"))

    # Token tracking: use real prompt_tokens from API response each step.
    # No hardcoded model limits — the API tells us the truth.
    last_prompt_tokens = 0   # updated every step from API response
    print(f"[AGENT] Budgets: prompt_compress={int(AGENT_TOKEN_COMPRESS*100)}% prompt_conclude={int(AGENT_TOKEN_CONCLUDE*100)}% timeout={AGENT_TIMEOUT_SEC}s")

    # Load agentic session state (shared scratchpad across iterations)
    _agent_session_id = str(msg.chat_id) if hasattr(msg, "chat_id") and msg.chat_id else "default"
    _agent_state: dict = {"session_id": _agent_session_id}  # always inject session_id
    # Auto-init session in Supabase if not exists
    try:
        from core_tools import TOOLS as _T
        _sinit = _T.get("agent_session_init", {}).get("fn")
        if _sinit:
            _init_r = _sinit(session_id=_agent_session_id, goal=goal[:200], chat_id=_agent_session_id)
            print(f"[AGENT] session_init: {_init_r.get('action','?')} id={_agent_session_id}")
        _loop_start_ts = __import__("datetime").datetime.utcnow().isoformat()[:19]  # ts anchor for fresh-step filter
        _sg = _T.get("agent_state_get", {}).get("fn")
        if _sg:
            _sr = _sg(session_id=_agent_session_id)
            loaded = _sr.get("state", {}) if _sr.get("ok") else {}
            _agent_state.update(loaded)
        if msg.context.get("evidence_gate"):
            _agent_state.setdefault("evidence_gate", msg.context.get("evidence_gate", {}))
    except Exception as _se:
        print(f"[AGENT] state load error (non-fatal): {_se}")

    while True:
        step += 1
        elapsed = time.monotonic() - start_time

        # ── Early done: all steps completed THIS run (use _loop_start_ts to exclude stale steps)
        try:
            from core_tools import TOOLS as _TED
            _sget = _TED.get("agent_state_get", {}).get("fn")
            if _sget and step > 5:  # don't check before at least 5 steps run
                _sd = _sget(session_id=str(_agent_session_id))
                _done_steps = _sd.get("completed_steps", []) if _sd.get("ok") else []
                # Only count steps logged AFTER this loop started (not from previous runs)
                _fresh = [s for s in _done_steps if isinstance(s, dict)
                          and s.get("ts", "") >= _loop_start_ts]
                if len(_fresh) >= 10:
                    print(f"[AGENT] step={step} auto-done: {len(_fresh)} fresh steps done — forcing LLM conclusion")
                    # Don't build raw dict report — force the LLM to write the proper answer
                    # with full 4000 token budget so it formats data correctly
                    force_conclude = True
                    # Inject a clear summary of what was done so LLM has it
                    _step_summary = ", ".join(
                        f"{_s.get('step','?')}={str(_s.get('result',''))[:60]}"
                        for _s in _fresh[:10]
                    )
                    history.append({
                        "type": "thought_only",
                        "thought": (
                            f"ALL {len(_fresh)} STEPS COMPLETED THIS RUN. "
                            f"Steps: {_step_summary[:400]}. "
                            f"State: {str(_sd.get('state',{}))[:200]}. "
                            f"Return type=done NOW with full formatted report of all step results."
                        ),
                        "step": step,
                    })
        except Exception:
            pass  # non-fatal — never block loop on state read error

        # ── Termination: wall-clock timeout ───────────────────────────────────
        if elapsed > AGENT_TIMEOUT_SEC:
            print(f"[AGENT] Timeout after {elapsed:.0f}s at step {step}")
            force_conclude = True  # fall through to conclusion

        # ── Build prompt + check token budget ─────────────────────────────────
        prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=force_conclude, agent_state=_agent_state)
        char_count = _chars(prompt)

        # Use real token count from last API call if available, else estimate from chars
        effective_tokens = last_prompt_tokens if last_prompt_tokens > 0 else char_count // 4
        compress_threshold = int(AGENT_TOKEN_BUDGET * AGENT_TOKEN_COMPRESS // 4)  # convert budget to tokens
        conclude_threshold = int(AGENT_TOKEN_BUDGET * AGENT_TOKEN_CONCLUDE // 4)

        if not force_conclude and effective_tokens > compress_threshold:
            # More aggressive compression at higher token counts
            _keep = 4 if effective_tokens > 12000 else 6
            print(f"[AGENT] step={step} compressing history (tokens={effective_tokens} > {compress_threshold}, keep_last={_keep})")
            compressed_summary, history = _compress_history(history, keep_last=_keep)
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=False, agent_state=_agent_state)
            char_count = _chars(prompt)

        if not force_conclude and effective_tokens > conclude_threshold:
            print(f"[AGENT] step={step} token budget critical (tokens={effective_tokens} > {conclude_threshold}) — forcing conclusion")
            force_conclude = True
            prompt = _build_prompt(goal, history, compressed_summary, tools_summary, conclude=True, agent_state=_agent_state)

        print(f"[AGENT] step={step} prompt_real={last_prompt_tokens}t prompt_est≈{char_count//4}t elapsed={elapsed:.1f}s")

        # ── LLM think ──────────────────────────────────────────────────────────
        try:
            # Scale output tokens: more room when forced to conclude or deep in loop
            _out_tokens = 4000 if (force_conclude or step > 30) else 1500
            raw, last_prompt_tokens = _llm_think(_AGENT_SYSTEM, prompt, max_tokens=_out_tokens)
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
            if verification_goal and any(h.get("tool") == "shell" for h in history if h.get("type") == "action"):
                force_conclude = True
                history.append({
                    "type": "thought_only",
                    "thought": (
                        "Parse error during a verification goal after shell evidence was collected. "
                        "Conclude now from the deterministic shell results instead of exploring further."
                    ),
                    "step": step,
                })
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
            # Also count agent_step_done with same step_name
            if tool_name == "agent_step_done":
                sn = tool_args.get("step_name", "")
                same_step_calls = sum(
                    1 for h in history[-8:] if h.get("type") == "action"
                    and h.get("tool") == "agent_step_done"
                    and h.get("args", {}).get("step_name") == sn
                )
                if same_step_calls >= 2:
                    repeat_count = same_step_calls
            if repeat_count >= 2:
                print(f"[AGENT] step={step} repeat-guard {tool_name} (×{repeat_count}) — injecting cached result")
                # Find the last SUCCESSFUL result for this tool in history
                cached_result = None
                for h in reversed(history):
                    if h.get("tool") == tool_name and h.get("type") == "action" and h.get("result"):
                        cached_result = h.get("result")
                        break
                # Inject as if tool ran successfully — LLM sees data and can move on
                synthetic = {
                    "type": "action", "tool": tool_name, "args": tool_args,
                    "thought": f"[REPEAT-GUARD] Using cached result from earlier call — not re-executing.",
                    "result": cached_result or {"ok": True, "note": "cached — already ran this step"},
                    "summary": f"[CACHED] {tool_name} — data already retrieved, advancing",
                    "step": step,
                }
                history.append(synthetic)
                msg.add_tool_result(tool_name, True, cached_result or {"ok": True, "cached": True})
                # Auto-advance: save step to state so LLM knows it's done
                try:
                    from core_tools import TOOLS as _TRG
                    _sdone = _TRG.get("agent_step_done", {}).get("fn")
                    if _sdone and _agent_session_id:
                        _sdone(session_id=str(_agent_session_id),
                               step_name=f"auto_{tool_name}_{step}",
                               result="repeat-guard: cached result injected")
                        _agent_state[f"auto_step_{step}_done"] = tool_name
                except Exception:
                    pass
                # Do NOT increment consecutive_errors — this is expected behavior
                continue

            # Execute tool
            result = await _run_tool(tool_name, tool_args, msg)
            ok = result.get("ok", True) if isinstance(result, dict) else True
            print(f"[AGENT] step={step} {tool_name} ok={ok}")

            if ok and tool_name in _DISCOVERY_TOOLS:
                discovery_steps += 1
                if discovery_steps >= AGENT_DISCOVERY_STEP_LIMIT and not force_conclude:
                    force_conclude = True
                    print(
                        f"[AGENT] step={step} discovery budget reached "
                        f"({discovery_steps}/{AGENT_DISCOVERY_STEP_LIMIT}) — forcing conclusion"
                    )
                    history.append({
                        "type": "thought_only",
                        "thought": (
                            "Discovery budget reached. "
                            "Conclude now with the best evidence collected instead of listing more files."
                        ),
                        "step": step,
                    })

            if not ok:
                consecutive_errors += 1
                if consecutive_errors >= AGENT_ERROR_THRESHOLD:
                    msg.styled_response = (
                        f"⚠️ CORE agent halted: {consecutive_errors} consecutive tool failures "
                        f"at step {step}. Last failed: {tool_name}"
                    )
                    break
            else:
                # Don't reset error count for state-management tools or tools that
                # just had a repeat-guard trigger (they'd reset and loop forever)
                _STATE_TOOLS = {"agent_step_done", "agent_state_set", "agent_state_get", "agent_session_init"}
                # Only reset if this tool hasn't appeared in recent repeat-guards
                _recent_guard_tools = {
                    h.get("tool") for h in history[-6:]
                    if "[REPEAT-GUARD]" in str(h.get("thought", ""))
                }
                if tool_name not in _STATE_TOOLS and tool_name not in _recent_guard_tools:
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

            # Auto-save useful results to persistent state scratchpad
            if ok and isinstance(result, dict):
                try:
                    from core_tools import TOOLS as _T2
                    _sset = _T2.get("agent_state_set", {}).get("fn")
                    if _sset:
                        # Save IDs from insert/query results
                        if "id" in result:
                            _sset(session_id=str(_agent_session_id), key=f"{tool_name}_id", value=str(result["id"]))
                            _agent_state[f"{tool_name}_id"] = str(result["id"])
                        # Save first item ID from list results
                        data = result.get("data") or result.get("evolutions") or result.get("rows", [])
                        if isinstance(data, list) and data and isinstance(data[0], dict):
                            first_id = data[0].get("id")
                            if first_id:
                                _sset(session_id=str(_agent_session_id), key=f"{tool_name}_first_id", value=str(first_id))
                                _agent_state[f"{tool_name}_first_id"] = str(first_id)
                        # Save knowledge KB insert results
                        if tool_name in ("add_knowledge", "kb_update") and result.get("action") in ("inserted", "upserted"):
                            topic = tool_args.get("topic", "")
                            if topic:
                                _sset(session_id=str(_agent_session_id), key="last_kb_topic", value=topic)
                                _agent_state["last_kb_topic"] = topic
                        # Mark step done
                        _sdone = _T2.get("agent_step_done", {}).get("fn")
                        if _sdone:
                            _sdone(session_id=str(_agent_session_id), step_name=f"step_{step}_{tool_name}",
                                   result=str(result)[:200])
                except Exception as _ae:
                    pass  # state save is non-fatal — never block execution

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
