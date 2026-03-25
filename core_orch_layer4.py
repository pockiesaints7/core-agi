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
    "general_tool":     [],  # handled by smart dispatch or Groq — too varied for fast-path
    "general_query":    ["search_kb"],  # default: search KB first, then let Groq plan more if needed
    "task_execution":   [],  # always goes to Groq planner — too dynamic for fast-path
}

_PLAN_SYSTEM = (
    "You are the task planner for CORE — an autonomous AGI system running on an Oracle Cloud Ubuntu VM. "
    "CORE has a full tool registry (171+ tools) covering: Supabase DB operations, GitHub file ops, "
    "VM shell execution, Telegram notifications, knowledge base (KB) search/write, "
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

RULES:
- type must be "tool_execution" or "multi_step" for ANY non-trivial request
- Use "direct_response" ONLY for pure greetings like "hi" or "thanks"
- tool names must EXACTLY match the registry above
- For kb_query/search: use "search_kb" with args {{"query": "<search term>"}}
- For time: use "get_time" or "datetime_now"
- For system health: use "get_system_health"
- For state: use "get_state"
- For calculations: use "calc" with args {{"expr": "<expression>"}}
- For web search: use "web_search" with args {{"query": "<query>"}}
- For mistakes: use "get_mistakes"
- For evolutions: use "list_evolutions"
- For tasks: use "get_state" or "task_add"

Return JSON:
{{
  "type": "direct_response|tool_execution|multi_step",
  "subtasks": [
    {{"step": 1, "action": "description", "tool": "exact_tool_name", "args": {{}}, "expected_output": "description"}}
  ],
  "estimated_complexity": "low|medium|high",
  "requires_confirmation": false,
  "direct_answer": "only if type=direct_response, the actual answer text"
}}
"""


def _build_tool_list() -> tuple[str, int]:
    """Cached: only builds once per process lifetime.
    Returns (formatted_string, count) — tool name + short description per line,
    grouped by functional category so Groq can scan it reliably.
    """
    if _TOOL_LIST_CACHE_L4["list"] is not None:
        return _TOOL_LIST_CACHE_L4["list"], _TOOL_LIST_CACHE_L4["count"]

    try:
        from core_tools import TOOLS

        # Functional groups — human-readable categories Groq understands
        # Each group has a label and keyword matchers against tool name
        _GROUPS = [
            ("TIME/DATE",       ["get_time", "datetime_now"]),
            ("SYSTEM HEALTH",   ["get_system_health", "ping_health", "get_state", "get_state_key"]),
            ("KNOWLEDGE BASE",  ["search_kb", "add_knowledge", "kb_update", "ingest_knowledge",
                                  "get_behavioral_rules", "get_constitution", "search_mistakes",
                                  "semantic_kb_search"]),
            ("MISTAKES",        ["get_mistakes", "log_mistake", "mistakes_since"]),
            ("TASKS/GOALS",     ["task_add", "task_update", "get_active_goals", "set_goal",
                                  "update_goal_progress", "checkpoint"]),
            ("EVOLUTIONS",      ["list_evolutions", "approve_evolution", "reject_evolution",
                                  "bulk_reject_evolutions", "check_evolutions", "add_evolution_rule"]),
            ("TRAINING",        ["trigger_cold_processor", "get_training_pipeline",
                                  "get_quality_trend", "get_quality_alert", "log_quality_metrics"]),
            ("DEPLOY/RAILWAY",  ["deploy_status", "deploy_and_wait", "railway_logs_live",
                                  "railway_env_get", "railway_env_set", "railway_service_info",
                                  "redeploy", "build_status", "verify_live", "crash_report"]),
            ("CODE/FILES",      ["run_python", "shell", "read_file", "write_file", "file_read",
                                  "file_write", "file_list", "gh_read_lines", "gh_search_replace",
                                  "multi_patch", "patch_file", "smart_patch", "replace_fn",
                                  "core_py_fn", "core_py_validate", "core_py_rollback",
                                  "diff", "search_in_file", "append_to_file", "git"]),
            ("DATABASE",        ["sb_query", "sb_insert", "sb_bulk_insert", "sb_patch",
                                  "sb_upsert", "sb_delete", "get_table_schema"]),
            ("WEB",             ["web_search", "web_fetch", "summarize_url"]),
            ("UTILS",           ["calc", "weather", "currency", "translate", "datetime_now",
                                  "generate_image", "image_process", "convert_document",
                                  "create_document", "create_spreadsheet", "create_presentation",
                                  "read_document"]),
            ("NOTIFICATIONS",   ["notify_owner"]),
            ("SESSION",         ["session_start", "session_end", "update_state", "checkpoint",
                                  "log_reasoning", "cognitive_load"]),
            ("CRYPTO",          ["crypto_price", "crypto_balance", "crypto_trade"]),
            ("MONITORING",      ["listen", "listen_result", "vm_info", "get_system_health",
                                  "system_map_scan", "tool_health_scan"]),
            ("PROJECTS",        ["project_list", "project_get", "project_search", "project_register",
                                  "project_index", "project_update_kb"]),
            ("OWNER PROFILE",   ["get_owner_profile", "add_owner_observation"]),
            ("SELF-IMPROVE",    ["add_evolution_rule", "synthesize_evolutions", "scope_tracker",
                                  "contradiction_check", "reason_chain", "decompose_task",
                                  "lookahead", "goal_check", "impact_model", "circuit_breaker",
                                  "assert_source", "loop_detect", "predict_failure"]),
        ]

        placed = set()
        lines = []
        for group_label, tool_names in _GROUPS:
            # Filter to tools that actually exist in registry
            group_tools = []
            for tn in tool_names:
                if tn in TOOLS and tn not in placed:
                    entry = TOOLS[tn]
                    desc = ""
                    if isinstance(entry, dict):
                        desc = (entry.get("desc") or "")[:70]
                    elif hasattr(entry, "__doc__") and entry.__doc__:
                        desc = entry.__doc__.strip().split("\n")[0][:70]
                    group_tools.append(f"  {tn}: {desc}" if desc else f"  {tn}")
                    placed.add(tn)
            if group_tools:
                lines.append(f"[{group_label}]")
                lines.extend(group_tools)

        # Dump any remaining unplaced tools under MISC
        misc = [tn for tn in TOOLS if tn not in placed]
        if misc:
            lines.append("[MISC]")
            lines.extend(f"  {tn}" for tn in sorted(misc))

        total = len(TOOLS)
        result = "\n".join(lines)
        _TOOL_LIST_CACHE_L4["list"] = result
        _TOOL_LIST_CACHE_L4["count"] = total
        return result, total

    except Exception as e:
        # Graceful fallback — use real TOOLS registry key names
        fallback = (
            "- state/health: get_state, get_system_health, deploy_status\n"
            "- knowledge: search_kb, add_knowledge, get_mistakes, log_mistake, kb_update\n"
            "- tasks: get_state, checkpoint, task_add, task_update\n"
            "- training: get_training_pipeline, trigger_cold_processor, list_evolutions\n"
            "- code/files: read_file, write_file, multi_patch, patch_file, gh_search_replace\n"
            "- deploy: deploy_and_wait, railway_logs_live, redeploy, verify_live, build_status\n"
            "- notifications: notify_owner\n"
            "- web/tools: web_search, web_fetch, calc, datetime_now, weather, currency, translate\n"
            "- monitoring: listen, listen_result, get_time\n"
            "- system: run_python, shell, vm_info, file_list, file_read, file_write"
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

    # 1. No tools needed — ONLY for pure greetings/conversation
    if not classification.get("requires_tools", False) and msg.intent in ("conversation", "greeting"):
        return {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low",
            "direct_answer": None,
        }

    # 2. Fast-path single-tool intents (non-empty tool list only)
    if intent in _INTENT_TOOL_MAP:
        tools = _INTENT_TOOL_MAP[intent]
        if tools:  # only use fast-path if tools list is non-empty
            cmd_args = msg.context.get("command_args", "")
            text_lower = msg.text.lower()
            # Build smart args from message text for search-type tools
            smart_args: dict = {}
            if tools[0] == "list_tools":
                # Extract limit from text if user asks for a specific count
                import re as _re
                m = _re.search(r'\b(\d+)\b', msg.text)
                limit = m.group(1) if m else "20"
                smart_args = {"limit": limit}
            elif tools[0] in ("search_kb",) and msg.text:
                # Strip slash-command prefix for KB searches
                query_text = msg.text.strip()
                if query_text.startswith("/"):
                    query_text = " ".join(query_text.split()[1:])
                smart_args = {"query": query_text or msg.text}
            elif cmd_args:
                # For most tools, map cmd_args to the right param name
                _arg_map = {
                    "search_kb": "query", "web_search": "query",
                    "calc": "expression", "weather": "location",
                    "get_time": "timezone", "datetime_now": "timezone",
                }
                param = _arg_map.get(tools[0], "args")
                smart_args = {param: cmd_args}
            return {
                "type": "tool_execution",
                "subtasks": [
                    {
                        "step": i + 1,
                        "action": f"Execute {t}",
                        "tool": t,
                        "args": smart_args,
                        "expected_output": "tool result",
                    }
                    for i, t in enumerate(tools)
                ],
                "estimated_complexity": "low",
                "requires_confirmation": False,
            }

    # 2b. Smart fast-path for general_tool intent — resolve tool from command or text
    if intent == "general_tool":
        cmd = msg.context.get("command", "")
        cmd_args = msg.context.get("command_args", "").strip()
        text_lower = msg.text.lower()
        # Map command → tool directly
        _cmd_to_tool = {
            "/time": ("get_time", {"timezone": "Asia/Jakarta"}),
            "/calc": ("calc", {"expression": cmd_args} if cmd_args else {"expression": msg.text}),
            "/weather": ("weather", {"location": cmd_args} if cmd_args else {"location": "Jakarta"}),
            "/run": ("run_python", {"code": cmd_args} if cmd_args else {}),
        }
        if cmd in _cmd_to_tool:
            tool_name, tool_args = _cmd_to_tool[cmd]
            return {
                "type": "tool_execution",
                "subtasks": [{"step": 1, "action": f"Execute {tool_name}", "tool": tool_name,
                              "args": tool_args, "expected_output": "tool result"}],
                "estimated_complexity": "low",
                "requires_confirmation": False,
            }
        # Text-based detection for general_tool
        if any(w in text_lower for w in ("time", "date", "day", "clock")):
            tz = "Asia/Jakarta"  # owner default
            return {"type": "tool_execution", "subtasks": [
                {"step": 1, "action": "Get current time", "tool": "get_time",
                 "args": {"timezone": tz}, "expected_output": "current time"}
            ], "estimated_complexity": "low", "requires_confirmation": False}
        if any(w in text_lower for w in ("calculat", "compute", "math", " + ", " - ", " * ", " / ", "=")):
            expr = cmd_args or msg.text
            return {"type": "tool_execution", "subtasks": [
                {"step": 1, "action": "Calculate", "tool": "calc",
                 "args": {"expression": expr}, "expected_output": "calculation result"}
            ], "estimated_complexity": "low", "requires_confirmation": False}
        if any(w in text_lower for w in ("weather", "temperature", "forecast", "rain", "humid")):
            loc = cmd_args or "Jakarta"
            return {"type": "tool_execution", "subtasks": [
                {"step": 1, "action": "Get weather", "tool": "weather",
                 "args": {"location": loc}, "expected_output": "weather data"}
            ], "estimated_complexity": "low", "requires_confirmation": False}
        # Fallback for general_tool: let Groq plan it (falls through)

    # 3. Groq planning for complex/multi-step tasks
    # Inject live TOOLS registry so Groq knows every tool available
    tool_list, tool_count = _build_tool_list()
    try:
        # Inject L3 tool_hints as a priority signal so Groq doesn't have to
        # scan all 171 tools when L3 already identified strong candidates
        hints = classification.get("tool_hints", [])
        hints_str = ""
        if hints:
            try:
                from core_tools import TOOLS
                valid = [h for h in hints if h in TOOLS]
                if valid:
                    hints_str = f"\nPRIORITY TOOL HINTS FROM CLASSIFIER: {valid}\nUse these first if they fit the request.\n"
            except Exception:
                pass

        prompt = _PLAN_TEMPLATE.format(
            text=msg.text[:600],
            intent=intent,
            tier=msg.tier,
            domain=msg.context.get("current_domain", "general"),
            command=msg.context.get("command", ""),
            args=msg.context.get("command_args", ""),
            tool_list=tool_list,
            tool_count=tool_count or "?",
        ) + hints_str
        raw = groq_chat(
            system=_PLAN_SYSTEM,
            user=prompt,
            model=GROQ_MODEL,
            max_tokens=512,
        )
        plan = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        # Safety net: if Groq returns direct_response but requires_tools=True, override
        if plan.get("type") == "direct_response" and classification.get("requires_tools", False):
            print(f"[L4] Groq returned direct_response but requires_tools=True — overriding with tool fallback")
            raise ValueError("Groq plan mismatch: direct_response with requires_tools=True")
        print(f"[L4] Groq plan: type={plan.get('type')}  steps={len(plan.get('subtasks', []))}  tools_injected={tool_count}")
        return plan
    except Exception as exc:
        print(f"[L4] Groq planning failed (non-fatal): {exc}")
        # SAFE FALLBACK: use tool_hints from L3 if available, else search_kb as default
        hints = classification.get("tool_hints", [])
        if hints:
            # L3 gave us tool hints — use them directly
            valid_hints = []
            try:
                from core_tools import TOOLS
                valid_hints = [h for h in hints if h in TOOLS]
            except Exception:
                valid_hints = hints[:2]
            if valid_hints:
                return {
                    "type": "tool_execution",
                    "subtasks": [
                        {"step": i+1, "action": f"Execute {t}", "tool": t,
                         "args": {"query": msg.text} if "search" in t or "kb" in t else {},
                         "expected_output": "tool result"}
                        for i, t in enumerate(valid_hints[:3])
                    ],
                    "estimated_complexity": "low",
                    "requires_confirmation": False,
                    "_fallback": "tool_hints",
                }
        # Last resort: get_state gives CORE a chance to answer from session context
        return {
            "type": "tool_execution",
            "subtasks": [
                {"step": 1, "action": "Get current CORE state", "tool": "get_state",
                 "args": {}, "expected_output": "system state"}
            ],
            "estimated_complexity": "low",
            "_fallback": "groq_failed",
            "_error": str(exc),
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
