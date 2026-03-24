"""
core_orch_layer4.py — L4: Reasoning & Planning
Cognitive pre-flight checks + real Groq execution planning.
No mocks.
"""
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_MODEL, GROQ_FAST

# Destructive action keywords requiring owner tier
_DESTRUCTIVE_KW = frozenset([
    "delete", "remove", "drop", "destroy", "force", "rollback",
    "purge", "wipe", "reset", "truncate",
])

# Intent → tool mapping for simple one-tool dispatches
_INTENT_TOOL_MAP: Dict[str, List[str]] = {
    "system_health":    ["t_health"],
    "system_state":     ["t_state"],
    "task_list":        ["t_session_start"],
    "evolution_list":   ["t_list_evolutions"],
    "kb_search":        ["t_search_kb"],
    "mistake_list":     ["t_get_mistakes"],
    "trigger_training": ["t_trigger_cold_processor"],
    "trigger_cold":     ["t_trigger_cold_processor"],
    "deploy_status":    ["t_deploy_and_wait"],
    "listen_mode":      ["t_listen"],
    "checkpoint":       ["t_checkpoint"],
}

_PLAN_SYSTEM = (
    "You are CORE AGI's task planner. You decompose user requests into tool execution steps. "
    "Return ONLY valid JSON. No preamble, no markdown."
)

_PLAN_TEMPLATE = """
USER REQUEST: {text}
INTENT: {intent}
TIER: {tier}
DOMAIN: {domain}
COMMAND: {command}
COMMAND_ARGS: {args}

Available tool categories:
- State/health: t_state, t_health, t_session_start, t_ping_health
- Knowledge: t_search_kb, t_add_knowledge, t_get_mistakes, t_log_mistake
- Tasks: t_session_start, t_session_end, t_checkpoint
- Training: t_get_training_pipeline, t_trigger_cold_processor, t_list_evolutions, t_check_evolutions
- Code/Files: t_read_file, t_write_file, t_gh_search_replace, t_multi_patch, t_gh_read_lines
- Deployment: t_deploy_and_wait, t_railway_logs_live, t_core_py_validate, t_core_py_rollback
- Notifications: t_notify
- Monitoring: t_listen, t_listen_result

Return JSON:
{{
  "type": "direct_response|tool_execution|multi_step",
  "subtasks": [
    {{"step": 1, "action": "description", "tool": "t_tool_name", "args": {{}}, "expected_output": "description"}}
  ],
  "estimated_complexity": "low|medium|high",
  "requires_confirmation": false,
  "direct_answer": "only if type=direct_response, the actual answer text"
}}
"""


async def _cognitive_preflight(msg: OrchestratorMessage) -> Dict[str, Any]:
    """Run pre-flight safety and context checks."""
    checks = {"passed": True, "warnings": [], "blockers": []}

    # Destructive action check
    text_lower = msg.text.lower()
    if any(kw in text_lower for kw in _DESTRUCTIVE_KW):
        if msg.tier != "owner":
            checks["blockers"].append("Destructive action requires owner tier")
            checks["passed"] = False
        else:
            checks["warnings"].append("Destructive keyword detected — confirm before execution")

    # Context quality check
    if not msg.context.get("session"):
        checks["warnings"].append("No session context loaded")

    # Intent confidence check
    intent_data = msg.context.get("intent_classification", {})
    conf = intent_data.get("confidence", 1.0)
    if conf < 0.6:
        checks["warnings"].append(f"Low intent confidence ({conf:.2f}) — may misplan")

    if checks["warnings"]:
        print(f"[L4] Pre-flight warnings: {checks['warnings']}")
    if checks["blockers"]:
        print(f"[L4] Pre-flight BLOCKED: {checks['blockers']}")

    return checks


async def _build_plan(msg: OrchestratorMessage) -> Dict[str, Any]:
    """Build execution plan. Uses fast-path map first, Groq as fallback."""
    intent = msg.intent or "general_query"
    classification = msg.context.get("intent_classification", {})

    # 1. No tools needed
    if not classification.get("requires_tools", False):
        return {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low",
            "direct_answer": None,
        }

    # 2. Fast-path single-tool intents
    if intent in _INTENT_TOOL_MAP:
        tools = _INTENT_TOOL_MAP[intent]
        cmd_args = msg.context.get("command_args", "")
        return {
            "type": "tool_execution",
            "subtasks": [
                {
                    "step": i + 1,
                    "action": f"Execute {t}",
                    "tool": t,
                    "args": {"args": cmd_args} if cmd_args else {},
                    "expected_output": "tool result",
                }
                for i, t in enumerate(tools)
            ],
            "estimated_complexity": "low",
            "requires_confirmation": False,
        }

    # 3. Groq planning for complex/multi-step tasks
    try:
        prompt = _PLAN_TEMPLATE.format(
            text=msg.text[:600],
            intent=intent,
            tier=msg.tier,
            domain=msg.context.get("current_domain", "general"),
            command=msg.context.get("command", ""),
            args=msg.context.get("command_args", ""),
        )
        raw = groq_chat(
            system=_PLAN_SYSTEM,
            user=prompt,
            model=GROQ_MODEL,
            max_tokens=512,
        )
        plan = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        print(f"[L4] Groq plan: type={plan.get('type')}  steps={len(plan.get('subtasks', []))}")
        return plan
    except Exception as exc:
        print(f"[L4] Groq planning failed (non-fatal): {exc}")
        return {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "unknown",
            "error": str(exc),
        }


# ── Main layer ────────────────────────────────────────────────────────────────
async def layer_4_reason(msg: OrchestratorMessage):
    """
    Run pre-flight checks, build execution plan, hand to L5.
    """
    msg.track_layer("L4-START")
    print(f"[L4] Planning execution …")

    # Pre-flight
    preflight = await _cognitive_preflight(msg)
    msg.context["preflight_checks"] = preflight

    if not preflight["passed"]:
        msg.add_error("L4", Exception(f"Pre-flight blocked: {preflight['blockers']}"), "PREFLIGHT_BLOCKED")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(msg)
        return

    # Build plan
    plan = await _build_plan(msg)
    msg.plan = plan
    msg.context["execution_plan"] = plan

    msg.track_layer("L4-COMPLETE")
    print(f"[L4] Plan ready: type={plan.get('type')}  complexity={plan.get('estimated_complexity')}")

    from core_orch_layer5 import layer_5_tools
    await layer_5_tools(msg)
