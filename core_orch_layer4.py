"""
core_orch_layer4.py — L4: Reasoning & Planning
Cognitive pre-flight checks + real Groq execution planning.
No mocks.
"""
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage
from core_orch_context import build_decision_packet, build_evidence_gate, tool_result_has_evidence
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
- If request contains "then", "and then", "after that" → type MUST be "multi_step" with 2+ subtasks
- For kb_query/search: use "search_kb" with args {{"query": "<search term>"}}
- For time: use "get_time" or "datetime_now"
- For system health: use "get_system_health"
- For state/session/tasks: use "get_state"
- For calculations: use "calc" with args {{"expression": "<expression>"}}
- For web search: use "web_search" with args {{"query": "<query>"}}
- For listing VM files/directories: use "file_list" with args {{"path": "<path>", "pattern": "*"}}
- For VM shell/bash commands: use "shell" with args {{"command": "<bash command>"}}
- For running Python code: use "run_python" with args {{"code": "<python code>"}}
- For adding to knowledge base: use "add_knowledge" with args {{"domain": "<domain>", "topic": "<topic>", "content": "<content>"}}
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
                # Pass search="" to get all tools; L9 will pick top 10 from the result
                smart_args = {"search": "", "category": ""}
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


def _build_evidence_retrieval_plan(msg: OrchestratorMessage) -> Dict[str, Any]:
    gate = msg.evidence_gate or msg.context.get("evidence_gate") or {}
    query = (gate.get("search_query") or msg.text or "").strip()
    tools = []
    public_sources = gate.get("public_sources") or []

    # Always sweep Supabase/KB first unless this is a pure state request.
    if gate.get("retrieval_mode") != "state_only":
        tools.append({
            "step": len(tools) + 1,
            "action": "Search CORE memory for evidence",
            "tool": "search_kb",
            "args": {
                "query": query,
                "domain": msg.context.get("current_domain", "general"),
                "limit": "5",
            },
            "expected_output": "relevant KB hits",
            "evidence_stage": "supabase",
            "blocking": False,
        })

    # Repo map probe when the user is clearly asking about code/repo status.
    if gate.get("repo_map_needed") or gate.get("code_targets") or gate.get("retrieval_mode") == "code":
        repo_query = query or msg.text or ""
        repo_path = (gate.get("code_targets") or [None])[0] or ""
        tools.append({
            "step": len(tools) + 1,
            "action": "Load repository semantic map status",
            "tool": "repo_map_status",
            "args": {"scope": "summary", "limit": "10"},
            "expected_output": "repo map counts and latest scan",
            "evidence_stage": "repo_map",
            "blocking": False,
        })
        tools.append({
            "step": len(tools) + 1,
            "action": "Read repository component packet",
            "tool": "repo_component_packet",
            "args": {"path": repo_path, "query": repo_query, "limit": "10"},
            "expected_output": "repo component packet",
            "evidence_stage": "repo_map",
            "blocking": False,
        })
        tools.append({
            "step": len(tools) + 1,
            "action": "Build repository dependency graph",
            "tool": "repo_graph_packet",
            "args": {"path": repo_path, "query": repo_query, "depth": "2", "limit": "10"},
            "expected_output": "repo graph packet",
            "evidence_stage": "repo_map",
            "blocking": False,
        })
        tools.append({
            "step": len(tools) + 1,
            "action": "Check repo status before answering",
            "tool": "git",
            "args": {"repo_path": "/home/ubuntu/core-agi", "operation": "status"},
            "expected_output": "git status output",
            "evidence_stage": "local_code",
            "blocking": False,
        })
        for target in (gate.get("code_targets") or [])[:2]:
            tools.append({
                "step": len(tools) + 1,
                "action": f"Read code artifact {target}",
                "tool": "read_file",
                "args": {"path": target, "repo": "core-agi"},
                "expected_output": "file content",
                "evidence_stage": "local_code",
                "blocking": False,
            })

    # Public research sweep for public/current/latest/research/doc queries.
    if gate.get("public_research_needed") or gate.get("retrieval_mode", "").startswith("public_research"):
        sources = ",".join(public_sources[:6]) or "all"
        tools.append({
            "step": len(tools) + 1,
            "action": "Research public sources and enrich CORE memory",
            "tool": "ingest_knowledge",
            "args": {
                "topic": query,
                "sources": sources,
                "max_per_source": "12",
                "since_days": "30",
            },
            "expected_output": "public research summary and KB writes",
            "evidence_stage": "public_research",
            "blocking": False,
        })
        tools.append({
            "step": len(tools) + 1,
            "action": "Re-check CORE memory after public research",
            "tool": "search_kb",
            "args": {
                "query": query,
                "domain": msg.context.get("current_domain", "general"),
                "limit": "5",
            },
            "expected_output": "KB hits after public ingestion",
            "evidence_stage": "supabase_post_public",
            "blocking": False,
        })

    # Web sweep for anything needing public evidence.
    if gate.get("retrieval_mode") in {"supabase_then_web", "code_then_web"} or gate.get("needs_retrieval"):
        tools.append({
            "step": len(tools) + 1,
            "action": "Search the web for external evidence",
            "tool": "web_search",
            "args": {"query": query, "max_results": "5"},
            "expected_output": "web search results",
            "evidence_stage": "web",
            "blocking": False,
        })

    return {
        "type": "multi_step" if len(tools) > 1 else "tool_execution",
        "subtasks": tools,
        "estimated_complexity": "medium" if len(tools) > 1 else "low",
        "requires_confirmation": False,
        "stop_on_failure": False,
        "evidence_gate": gate,
        "direct_answer": None,
        "retrieval": True,
    }


# ── Main layer ────────────────────────────────────────────────────────────────
async def layer_4_reason(msg: OrchestratorMessage):
    """
    Run pre-flight checks, then either:
    - AGENTIC MODE: hand to core_orch_agent.run_agent_loop for complex multi-step tasks
    - FAST MODE: build static plan and hand to L5 (existing behaviour, unchanged)
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

    # Ensure decision packet is present
    if not msg.decision_packet:
        try:
            decision = build_decision_packet(msg)
            msg.decision_packet = decision
            msg.request_kind = decision.get("request_kind", msg.request_kind)
            msg.response_mode = decision.get("response_mode", msg.response_mode)
            msg.route_reason = decision.get("route_reason", msg.route_reason)
            msg.clarification_needed = bool(decision.get("clarification_needed", False))
            msg.context["request_kind"] = msg.request_kind
            msg.context["response_mode"] = msg.response_mode
            msg.context["route_reason"] = msg.route_reason
            msg.context["clarification_needed"] = msg.clarification_needed
            msg.context["decision_packet"] = decision
        except Exception as exc:
            print(f"[L4] decision_packet build failed (non-fatal): {exc}")

    if not msg.evidence_gate:
        try:
            gate = build_evidence_gate(msg)
            msg.evidence_gate = gate
            msg.context["evidence_gate"] = gate
        except Exception as exc:
            print(f"[L4] evidence_gate build failed (non-fatal): {exc}")

    # ── Agentic mode check ────────────────────────────────────────────────────
    # Only activate for complex requests — simple queries stay on fast path
    try:
        from core_orch_agent import is_agentic_request, run_agent_loop, AGENT_MODEL
        decision = msg.decision_packet or {}
        agentic_hint = bool(decision.get("agentic_hint", False))
        if msg.response_mode == "agentic" or agentic_hint or is_agentic_request(msg.text, msg.intent or ""):
            model_label = AGENT_MODEL or "groq"
            print(f"[L4] AGENTIC MODE activated model={model_label} intent={msg.intent}")
            msg.track_layer("L4-AGENTIC")
            msg.delegation_target = "agentic"
            msg.context["delegation_target"] = "agentic"
            await run_agent_loop(msg, goal=msg.text)
            return
    except ImportError as e:
        print(f"[L4] core_orch_agent not available (non-fatal): {e}")
    # ─────────────────────────────────────────────────────────────────────────

    # Clarification path (no tools)
    if msg.clarification_needed:
        msg.plan = {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low",
            "direct_answer": (msg.decision_packet or {}).get("clarification_prompt")
            or "I need more detail to proceed. What exactly should I do, and what is the expected outcome?",
        }
        msg.track_layer("L4-CLARIFY")
        from core_orch_layer5 import layer_5_tools
        await layer_5_tools(msg)
        return

    gate = msg.evidence_gate or msg.context.get("evidence_gate", {})

    # Capability/status/review/debug paths can still retrieve evidence if the gate says so.
    if msg.response_mode in ("status", "capability", "review", "debug") and not gate.get("needs_retrieval"):
        msg.plan = {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low",
            "direct_answer": None,
        }
        msg.track_layer("L4-DIRECT")
        from core_orch_layer5 import layer_5_tools
        await layer_5_tools(msg)
        return

    # Evidence gate: if Supabase is sparse, do a bounded evidence sweep before answering.
    if gate.get("needs_retrieval"):
        msg.plan = _build_evidence_retrieval_plan(msg)
        msg.context["execution_plan"] = msg.plan
        msg.context["evidence_gate"] = gate
        msg.track_layer("L4-EVIDENCE")
        print(
            f"[L4] Evidence sweep enabled: mode={gate.get('retrieval_mode')} "
            f"score={gate.get('score')} tools={len(msg.plan.get('subtasks', []))}"
        )
        from core_orch_layer5 import layer_5_tools
        await layer_5_tools(msg)
        return

    # Build static plan (existing fast-path)
    plan = await _build_plan(msg)
    msg.plan = plan
    msg.context["execution_plan"] = plan

    msg.track_layer("L4-COMPLETE")
    print(f"[L4] Plan ready: type={plan.get('type')}  complexity={plan.get('estimated_complexity')}")

    from core_orch_layer5 import layer_5_tools
    await layer_5_tools(msg)
