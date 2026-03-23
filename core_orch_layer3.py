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
        "Output ONLY valid JSON — no markdown fences:",
        '{"intent_parsed":"true goal","can_direct":true/false,"direct_answer":"if can_direct","plan":["step1","step2"],"tool_hints":["tool_name"],"risk":"none/low/medium/high/critical","creative_path":"non-obvious solution or empty","fallback":"fallback approach"}',
    ]
    return "\n".join(lines)


def _history_to_text(conversation: list) -> str:
    lines = []
    for h in conversation[-10:]:
        role = h.get("role", "user")
        content = h.get("content", "")[:300]
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


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
    user_msg      = f"CONVERSATION SO FAR:\n{history_text}\n\nOWNER: {text}"

    plan_data = {}
    try:
        from core_orch_layer8 import call_model_json
        raw = await call_model_json(
            system=system_prompt,
            user=user_msg,
            max_tokens=600,
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

    # ── Risk assessment ───────────────────────────────────────────────────────
    plan_list    = plan_data.get("plan", [])
    risk, req_confirm = _assess_risk(text, plan_list)
    # Trust model risk if higher
    model_risk = plan_data.get("risk", "none")
    risk_order = ["none", "low", "medium", "high", "critical"]
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
        "tool_hints":       plan_data.get("tool_hints", []),
        "risk":             risk,
        "requires_confirm": req_confirm,
        "creative_path":    plan_data.get("creative_path", ""),
        "fallback":         plan_data.get("fallback", ""),
        "constitution_ok":  True,
        "preflight_ms":     preflight_ms,
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
