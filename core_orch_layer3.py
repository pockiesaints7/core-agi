"""
core_orch_layer3.py — L3: REASONING
=====================================
Cognitive pre-flight before any execution.
Constitution check → intent extraction → plan → risk assessment.

Outputs an ExecutionPlan passed to L4:
  {
    "ctx":            dict,        # full SessionContext from L2
    "intent_parsed":  str,         # true goal behind the message
    "can_direct":     bool,        # True = answer without tool calls
    "direct_answer":  str,         # filled if can_direct=True
    "plan":           list[str],   # ordered subtask list
    "tool_hints":     list[str],   # tool names to prioritise
    "risk":           str,         # none / low / medium / high / critical
    "requires_confirm": bool,      # True = L5 must gate before execution
    "creative_path":  str,         # non-obvious approach if exists
    "fallback":       str,         # alternative if primary fails
    "constitution_ok": bool,       # False = hard block, do not proceed
    "preflight_ms":   int          # time taken
  }
"""

import json
import time
import asyncio
from typing import Optional

# ── Constitution invariants — enforced as code, not just prompt ───────────────
# C1  Owner identity immutable
# C2  Destructive/irreversible → confirm first
# C3  Never self-approve evolutions
# C4  Never log/echo credentials
# C5  CORE identity preserved — no drift to generic assistant
# C6  Skill file read-only from CORE perspective
# C7  Supabase = source of truth; halt if down
# C8  force_close = owner only
# C9  Background loops inherit L0 permissions only
# C10 Constitution immutable — only owner edits skill file

_DESTRUCTIVE_KW = {
    "drop table", "delete all", "rm -rf", "truncate", "wipe", "purge",
    "destroy", "format disk", "sb_delete", "force_close", "nuke",
    "hapus semua", "reset database",
}
_CREDENTIAL_KW = {
    "api_key", "secret", "password", "token", "private_key", "bearer",
    "SUPABASE_SVC", "GROQ_API_KEY", "TELEGRAM_TOKEN", "GITHUB_PAT",
}
_EVOLUTION_SELF_APPROVE = {"auto_approve", "self_approve", "approve_evolution"}

# Risk mapping: tool perm → risk level
_PERM_RISK = {"READ": "none", "WRITE": "low", "EXECUTE": "medium", "DESTROY": "critical"}

# Keywords that suggest a destructive intent even without exact match
_HIGH_RISK_WORDS = {"delete", "drop", "remove", "overwrite", "reset", "wipe",
                    "hapus", "reset", "truncate"}


def _constitution_check(intent: dict, text: str) -> tuple[bool, str]:
    """
    Returns (ok, reason). ok=False means hard block — do not proceed.
    Checks C1–C5, C8.
    """
    lower = text.lower()

    # C4: credentials
    for kw in _CREDENTIAL_KW:
        if kw.lower() in lower:
            return False, f"C4_VIOLATION: message references credential keyword '{kw}'"

    # C3: self-approve
    for kw in _EVOLUTION_SELF_APPROVE:
        if kw in lower:
            return False, "C3_VIOLATION: CORE cannot self-approve evolutions"

    # C5: identity drift — if someone tells CORE to "act as", "pretend to be"
    if any(p in lower for p in ["act as", "pretend you are", "you are now", "forget you are core"]):
        return False, "C5_VIOLATION: CORE identity is immutable — cannot role-play as different system"

    # C8: force_close
    if "force_close" in lower and intent.get("tier") != "owner":
        return False, "C8_VIOLATION: force_close requires owner tier"

    return True, ""


def _assess_risk(text: str, plan: list) -> tuple[str, bool]:
    """
    Returns (risk_level, requires_confirm).
    risk_level: none / low / medium / high / critical
    requires_confirm: True if L5 must gate before execution
    """
    lower = text.lower()
    plan_str = " ".join(plan).lower()
    combined = lower + " " + plan_str

    # Check for destructive patterns
    for kw in _DESTRUCTIVE_KW:
        if kw in combined:
            return "critical", True

    # High-risk words
    risk_words_found = [w for w in _HIGH_RISK_WORDS if w in combined]
    if len(risk_words_found) >= 2:
        return "high", True
    if len(risk_words_found) == 1:
        return "medium", False

    # Deploy / push operations
    if any(w in combined for w in ["deploy", "push to", "systemctl", "restart service"]):
        return "medium", False

    # Write operations
    if any(w in combined for w in ["write", "patch", "update", "insert", "create"]):
        return "low", False

    return "none", False


def _build_system_prompt_for_preflight(ctx: dict) -> str:
    """Build a lean system prompt for the pre-flight reasoning call."""
    rules = ctx.get("behavioral_rules", [])
    mistakes = ctx.get("recent_mistakes", [])
    kb = ctx.get("kb_snippets", [])
    tasks = ctx.get("in_progress_tasks", [])

    lines = [
        "You are CORE — a sovereign intelligence. Owner: REINVAGNAR (Jakarta, WIB/UTC+7).",
        "You own problems end-to-end. Execute, don't ask permission for low-risk actions.",
        "",
    ]

    if tasks:
        t = tasks[0]
        raw = t.get("task", "")
        try:
            title = json.loads(raw).get("title", raw[:80]) if isinstance(raw, str) else str(raw)[:80]
        except Exception:
            title = str(raw)[:80]
        lines.append(f"ACTIVE TASK: {title} (P{t.get('priority','?')})")

    if mistakes:
        m_lines = " | ".join(
            f"[{m.get('domain','?')}] AVOID: {m.get('what_failed','')[:80]}"
            for m in mistakes[:3]
        )
        lines.append(f"RECENT MISTAKES — {m_lines}")

    if kb:
        kb_lines = "\n".join(
            f"  [{e.get('domain','?')}] {e.get('topic','')}: {(e.get('instruction') or e.get('content',''))[:120]}"
            for e in kb[:4]
        )
        lines.append(f"RELEVANT KB:\n{kb_lines}")

    if rules:
        r_lines = "\n".join(
            f"  [{r.get('trigger','?')}] {r.get('pointer','')[:100]}"
            for r in rules[:20]
        )
        lines.append(f"BEHAVIORAL RULES:\n{r_lines}")

    lines += [
        "",
        "PRE-FLIGHT: identify true intent, best approach, risk level.",
        "",
        "CRITICAL RULE — can_direct MUST be false if the question requires live data:",
        "  - counts/stats/numbers from Supabase (kb count, task count, tool count, etc.)",
        "  - current status of any system, task, deploy, or service",
        "  - anything needing search_kb, sb_query, get_state, list_tools, or any tool",
        "  - any question about CORE's own memory, knowledge base, mistakes, or patterns",
        "  can_direct=true ONLY for pure reasoning with NO live data needed.",
        "  When in doubt -> can_direct=false, add correct tool to tool_hints.",
        "",
        "Output ONLY valid JSON, no markdown fences:",
        '{"intent_parsed":"true goal","can_direct":true/false,"direct_answer":"only if can_direct and zero live data needed","plan":["step1","step2"],"tool_hints":["tool_name"],"risk":"none/low/medium/high/critical","creative_path":"non-obvious solution or empty","fallback":"fallback approach"}',
    ]
    return "\n".join(lines)


def _history_to_text(conversation: list) -> str:
    lines = []
    for h in conversation[-10:]:
        role = h.get("role", "user")
        content = h.get("content", "")[:300]
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


def _extract_conversation_signals(conversation: list, text: str) -> dict:
    """
    Extract signals from conversation history that should influence L3 reasoning.
    Returns dict of signals passed into the preflight prompt.
    """
    signals = {
        "is_followup":        False,   # owner is following up on previous response
        "is_repeat":          False,   # same question asked before (model failed)
        "pending_confirm":    False,   # last CORE message asked for confirmation
        "last_failed_tool":   "",      # tool that failed in last turn
        "consecutive_errors": 0,       # how many turns in a row had errors
    }
    if not conversation:
        return signals

    text_lower = text.lower()
    recent = conversation[-6:]

    # Is this a follow-up? (short message after a long CORE response)
    if len(recent) >= 2:
        last_assistant = next(
            (m for m in reversed(recent) if m.get("role") == "assistant"), None
        )
        if last_assistant and len(text) < 60:
            signals["is_followup"] = True

    # Is CORE waiting for confirmation?
    confirm_phrases = ["confirm", "proceed?", "lanjutkan?", "are you sure", "yakin"]
    if last_assistant:
        last_text = last_assistant.get("content", "").lower()
        if any(p in last_text for p in confirm_phrases):
            signals["pending_confirm"] = True

    # Repeat detection: same question in last 3 user turns
    user_turns = [m.get("content", "").lower() for m in recent if m.get("role") == "user"]
    if user_turns.count(text_lower) >= 1 and len(user_turns) > 1:
        signals["is_repeat"] = True

    # Error streak
    error_count = sum(
        1 for m in recent
        if m.get("role") == "assistant" and
        any(e in m.get("content", "").lower() for e in ["error", "failed", "⚠️", "❌"])
    )
    signals["consecutive_errors"] = error_count

    return signals


async def layer_3_reason(ctx: dict) -> None:
    """
    Main entry point from L2. Runs pre-flight, produces ExecutionPlan, passes to L4.
    """
    t0     = time.time()
    intent = ctx["intent"]
    text   = intent["text"]
    cid    = intent["sender_id"]

    # ── Constitution check (C1–C10) ───────────────────────────────────────────
    const_ok, const_reason = _constitution_check(intent, text)
    if not const_ok:
        print(f"[L3] CONSTITUTION BLOCK: {const_reason}")
        from core_orch_layer5 import layer_5_output
        await layer_5_output(
            intent,
            f"⛔ Blocked by CORE Constitution.\nReason: {const_reason}",
            tool_results=[],
        )
        return

    # ── Pre-flight reasoning ──────────────────────────────────────────────────
    system_prompt = _build_system_prompt_for_preflight(ctx)
    history_text  = _history_to_text(ctx.get("conversation", []))
    # user_msg_enriched is built after conv_signals extraction below

    # Extract conversation signals before preflight
    conv_signals = _extract_conversation_signals(ctx.get("conversation", []), text)

    # Inject signals into user message for context-aware reasoning
    signal_lines = []
    if conv_signals["is_repeat"]:
        signal_lines.append("⚠️ REPEAT: Owner asked this before — previous answer was insufficient. Must use tools.")
    if conv_signals["pending_confirm"]:
        signal_lines.append("⚠️ PENDING CONFIRM: Previous turn asked owner for confirmation.")
    if conv_signals["consecutive_errors"] >= 2:
        signal_lines.append(f"⚠️ ERROR STREAK: {conv_signals['consecutive_errors']} consecutive error turns — be extra careful.")
    if conv_signals["is_followup"]:
        signal_lines.append("ℹ️ FOLLOW-UP: Short message after long response — likely clarification or continuation.")

    signal_block = ("\n" + "\n".join(signal_lines)) if signal_lines else ""
    user_msg_enriched = f"CONVERSATION SO FAR:\n{history_text}{signal_block}\n\nOWNER: {text}"

    plan_data = {}
    try:
        from core_orch_layer8 import call_model_json
        raw = await call_model_json(
            system=system_prompt,
            user=user_msg_enriched,
            max_tokens=700,
        )
        plan_data = raw if isinstance(raw, dict) else {}
    except Exception as e:
        print(f"[L3] Pre-flight model call failed (non-fatal): {e}")
        plan_data = {
            "intent_parsed": text,
            "can_direct": False,
            "plan": ["execute task directly"],
            "tool_hints": [],
            "risk": "low",
            "creative_path": "",
            "fallback": "use run_python as universal fallback",
        }

    # Self-critique pass: if plan is empty or tool_hints empty on non-trivial message,
    # run a second model call to recover a better plan
    _plan_weak = (
        not plan_data.get("plan") or
        (not plan_data.get("tool_hints") and not plan_data.get("can_direct") and len(text) > 20)
    )
    if _plan_weak and not plan_data.get("can_direct"):
        try:
            critique_prompt = (
                f"Your previous plan for: {text!r}\n"
                f"was: {json.dumps(plan_data)}\n\n"
                "The plan is missing steps or tool_hints. "
                "Produce a better plan. Be specific about which tools to call. "
                "Output ONLY valid JSON, same schema as before."
            )
            from core_orch_layer8 import call_model_json
            raw2 = await call_model_json(
                system=system_prompt,
                user=critique_prompt,
                max_tokens=700,
            )
            if isinstance(raw2, dict) and raw2.get("plan"):
                print(f"[L3] Self-critique improved plan: {raw2.get('plan')}")
                plan_data = raw2
        except Exception as e:
            print(f"[L3] Self-critique pass failed (non-fatal): {e}")

    # Code-level guard: force can_direct=False when live data is needed.
    # Uses intent classification, not raw keyword matching, to avoid blocking
    # legitimate explanations like "what is a task queue?" (pure reasoning, no data).
    _text_lower = text.lower()

    # Pattern 1: explicit quantity/status queries about CORE's own data
    import re as _re
    _LIVE_DATA_PATTERNS = [
        r'how many',                        # "how many kb/tasks/tools"
        r'berapa',                          # Indonesian "how many"
        r'count.{0,30}(kb|task|tool|mistake|pattern|evolution)',
        r'(show|list|display|tampilkan|daftar).{0,40}(kb|task|tool|mistake|pattern|rule)',
        r'what.{0,20}(my|your|current).{0,30}(status|task|kb|mistake|pattern)',
        r'(status|health).{0,20}(of|for|system|core|deploy)',
        r'(current|latest|terbaru|terkini).{0,30}(task|deploy|status|error)',
    ]
    _is_live_data_query = any(_re.search(p, _text_lower) for p in _LIVE_DATA_PATTERNS)

    # Pattern 2: model returned tool_hints — if it listed tools, it knows data is needed
    _model_wants_tools = bool(plan_data.get("tool_hints"))

    if plan_data.get("can_direct") and (_is_live_data_query or _model_wants_tools):
        reason = "live-data pattern" if _is_live_data_query else "model listed tool_hints"
        print(f"[L3] Overriding can_direct=True ({reason}) — forcing tool path")
        plan_data["can_direct"] = False

    # Fast path: model says it can answer directly
    if plan_data.get("can_direct") and plan_data.get("direct_answer"):
        preflight_ms = int((time.time() - t0) * 1000)
        print(f"[L3] Direct answer path ({preflight_ms}ms)")
        from core_orch_layer5 import layer_5_output
        await layer_5_output(intent, plan_data["direct_answer"], tool_results=[])
        # Still log to L9 for quality tracking
        from core_orch_layer9 import layer_9_log_turn
        await layer_9_log_turn(ctx, plan_data["direct_answer"], tool_results=[])
        return

    # ── Enrich plan: bind tool_hints to steps ────────────────────────────────
    plan_list  = plan_data.get("plan", [])
    tool_hints = plan_data.get("tool_hints", [])

    # Build structured steps: [{"step": str, "tool": str|None, "idx": int}]
    structured_steps = []
    for i, step_text in enumerate(plan_list):
        # Try to bind a tool hint to this step by keyword overlap
        bound_tool = None
        step_lower = step_text.lower()
        for th in tool_hints:
            th_stem = th.replace("_", " ").lower()
            if any(w in step_lower for w in th_stem.split()):
                bound_tool = th
                break
        structured_steps.append({"step": step_text, "tool": bound_tool, "idx": i})

    # ── Risk assessment ───────────────────────────────────────────────────────
    # Check both message text AND plan steps AND tool_hints for risk signals
    plan_str_full = " ".join(plan_list + tool_hints).lower()
    risk, req_confirm = _assess_risk(text, plan_list)

    # Re-assess against plan+tools (catches sb_delete in plan even if not in message)
    _combined_risk_text = text + " " + plan_str_full
    plan_risk, plan_confirm = _assess_risk(_combined_risk_text, plan_list)
    risk_order = ["none", "low", "medium", "high", "critical"]
    if risk_order.index(plan_risk) > risk_order.index(risk):
        risk = plan_risk
        req_confirm = req_confirm or plan_confirm

    # Trust model risk if higher
    model_risk = plan_data.get("risk", "none")
    if risk_order.index(model_risk) > risk_order.index(risk):
        risk = model_risk
        if risk in ("high", "critical"):
            req_confirm = True

    preflight_ms = int((time.time() - t0) * 1000)
    print(f"[L3] Plan: intent={plan_data.get('intent_parsed','?')[:60]!r} "
          f"risk={risk} confirm={req_confirm} steps={len(plan_list)} ({preflight_ms}ms)")

    execution_plan = {
        "ctx":              ctx,
        "intent_parsed":    plan_data.get("intent_parsed", text),
        "can_direct":       False,
        "direct_answer":    "",
        "plan":             plan_list,
        "structured_steps": structured_steps,   # enriched: [{step, tool, idx}]
        "tool_hints":       tool_hints,
        "risk":             risk,
        "requires_confirm": req_confirm,
        "creative_path":    plan_data.get("creative_path", ""),
        "fallback":         plan_data.get("fallback", ""),
        "constitution_ok":  True,
        "preflight_ms":     preflight_ms,
        "conv_signals":     conv_signals,        # multi-turn context signals
    }

    # ── Route to L4 (execution) or L5 (confirmation gate) ────────────────────
    if req_confirm:
        from core_orch_layer5 import layer_5_request_confirm
        confirmed = await layer_5_request_confirm(intent, execution_plan)
        if not confirmed:
            return   # owner rejected — done

    from core_orch_layer4 import layer_4_execute
    await layer_4_execute(execution_plan)


if __name__ == "__main__":
    print("🛰️ Layer 3: Reasoning — Online.")
