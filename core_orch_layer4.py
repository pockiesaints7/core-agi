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
# IMPORTANT: keys must match TOOLS registry exactly (no t_ prefix)
_INTENT_TOOL_MAP: Dict[str, List[str]] = {
    "system_health":    ["get_system_health"],
    "system_state":     ["get_state"],
    "task_list":        ["get_state"],
    "evolution_list":   ["list_evolutions"],
    "kb_search":        ["search_kb"],
    "kb_query":         ["search_kb"],
    "mistake_list":     ["get_mistakes"],
    "trigger_training": ["trigger_cold_processor"],
    "trigger_cold":     ["trigger_cold_processor"],
    "deploy_status":    ["deploy_status"],
    "listen_mode":      ["listen"],
    "checkpoint":       ["checkpoint"],
    "list_tools":       ["list_tools"],
    "general_tool":     [],  # handled by Groq planning — too varied for fast-path
}

_PLAN_SYSTEM = (
    "You are the task planner for CORE — an autonomous AGI system deployed on an Ubuntu VM. "
    "CORE has a full tool registry (171+ tools) covering: Supabase DB operations, GitHub file ops, "
    "Railway deployments, Telegram notifications, knowledge base (KB) search/write, "
    "mistake logging, session management, web search, web fetch, Python execution, "
    "file operations, system health checks, crypto, weather, currency, image generation, and more. "
    "Your job: decompose the user request into tool execution steps. "
    "ALWAYS prefer tool_execution or multi_step over direct_response unless the request is pure small-talk. "
    "ANY request that asks about time, search, calculation, data lookup, system state, or task execution REQUIRES tools. "
    "Return ONLY valid JSON. No preamble, no markdown, no extra keys."
)

# Dynamic tool registry injected at call time — see _build_plan()
# GAP-NEW-10: module-level tool list cache so it is not rebuilt on every Groq call
_TOOL_LIST_CACHE_L4: dict = {"list": None, "count": 0}

_PLAN_TEMPLATE = """
USER REQUEST: {text}
INTENT: {intent}
TIER: {tier}
DOMAIN: {domain}
COMMAND: {command}
COMMAND_ARGS: {args}

Available tools (live registry — {tool_count} total):
{tool_list}

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


def _build_tool_list() -> tuple[str, int]:
    """Cached: only builds once per process lifetime."""
    if _TOOL_LIST_CACHE_L4["list"] is not None:
        return _TOOL_LIST_CACHE_L4["list"], _TOOL_LIST_CACHE_L4["count"]

    """
    Dynamically pull TOOLS keys from the live registry.
    Returns (formatted_string, count).
    Falls back to a static category summary on import error.
    """
    try:
        from core_tools import TOOLS
        from core_config import TOOL_CATEGORY_KEYWORDS

        # Group tools by category for a readable but compact prompt injection
        cats: Dict[str, List[str]] = {cat: [] for cat in TOOL_CATEGORY_KEYWORDS}
        cats["misc"] = []
        for tn in TOOLS.keys():
            placed = False
            for cat, kws in TOOL_CATEGORY_KEYWORDS.items():
                if any(kw in tn for kw in kws):
                    cats[cat].append(tn)
                    placed = True
                    break
            if not placed:
                cats["misc"].append(tn)

        lines = []
        for cat, tools in cats.items():
            if tools:
                lines.append(f"- {cat}: {', '.join(sorted(tools))}")

        total = len(TOOLS)
        result = "\n".join(lines)
        _TOOL_LIST_CACHE_L4["list"] = result
        _TOOL_LIST_CACHE_L4["count"] = total
        return result, total

    except Exception as e:
        # Graceful fallback — static summary
        fallback = (
            "- state/health: t_state, t_health, t_session_start, t_ping_health\n"
            "- knowledge: t_search_kb, t_add_knowledge, t_get_mistakes, t_log_mistake\n"
            "- tasks: t_session_start, t_session_end, t_checkpoint\n"
            "- training: t_get_training_pipeline, t_trigger_cold_processor, t_list_evolutions\n"
            "- code/files: t_read_file, t_write_file, t_multi_patch, t_patch_file\n"
            "- deploy: t_deploy_and_wait, t_railway_logs_live, t_redeploy, t_verify_live\n"
            "- notifications: t_notify\n"
            "- monitoring: t_listen, t_listen_result"
        )
        print(f"[L4] tool registry import failed, using static fallback: {e}")
        return fallback, 0


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
    # Inject live TOOLS registry so Groq knows every tool available
    tool_list, tool_count = _build_tool_list()
    try:
        prompt = _PLAN_TEMPLATE.format(
            text=msg.text[:600],
            intent=intent,
            tier=msg.tier,
            domain=msg.context.get("current_domain", "general"),
            command=msg.context.get("command", ""),
            args=msg.context.get("command_args", ""),
            tool_list=tool_list,
            tool_count=tool_count or "?",
        )
        raw = groq_chat(
            system=_PLAN_SYSTEM,
            user=prompt,
            model=GROQ_MODEL,
            max_tokens=512,
        )
        plan = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        print(f"[L4] Groq plan: type={plan.get('type')}  steps={len(plan.get('subtasks', []))}  tools_injected={tool_count}")
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
