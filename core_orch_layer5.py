"""
core_orch_layer5.py — L5: Tool Execution
Executes plan subtasks by calling actual tools from core_tools.TOOLS registry.
No simulated results. No mocks.
"""
import asyncio
import inspect
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage
from core_orch_context import tool_result_has_evidence

# ── TOOLS registry import ─────────────────────────────────────────────────────
# Lazy import to avoid circular issues at module load time
def _get_tools() -> Dict[str, Any]:
    try:
        from core_tools import TOOLS
        return TOOLS
    except ImportError as exc:
        print(f"[L5] WARNING: core_tools import failed: {exc}")
        return {}


# Tool name aliases to handle /command → TOOLS registry key mapping
# IMPORTANT: values must match TOOLS registry keys exactly (no t_ prefix)
_COMMAND_TOOL_ALIASES = {
    "/health":      "get_system_health",
    "/state":       "get_state",
    "/status":      "get_state",
    "/tasks":       "get_state",
    "/evolutions":  "list_evolutions",
    "/kb":          "search_kb",
    "/mistakes":    "get_mistakes",
    "/train":       "trigger_cold_processor",
    "/cold":        "trigger_cold_processor",
    "/deploy":      "deploy_status",
    "/listen":      "listen",
    "/checkpoint":  "checkpoint",
    "/ask":         "search_kb",
    "/search":      "search_kb",
    "/time":        "get_time",
    "/calc":        "calc",
    "/weather":     "weather",
    "/tools":       "list_tools",
    "/run":         "run_python",
}

# Trusted-tier blocked tools (destructive) — match TOOLS registry keys exactly
_TRUSTED_BLOCKED = frozenset([
    "write_file", "gh_search_replace", "multi_patch",
    "core_py_rollback", "sb_insert", "maintenance_purge",
    "railway_env_set", "approve_evolution", "reject_evolution",
    "bulk_reject_evolutions", "trigger_cold_processor",
    "session_end", "sb_delete", "sb_patch",
])


def _call_tool(tool_fn, args: Dict[str, Any]) -> Any:
    """
    Call a tool function synchronously.
    Handles both plain functions and coroutines.
    Strips unknown kwargs to avoid TypeError.
    NOTE: Must be called from a thread (via run_in_executor), never from
    inside a running event loop — coroutines are run via asyncio.run()
    which creates a fresh loop in the thread.
    """
    try:
        sig = inspect.signature(tool_fn)
        valid_params = set(sig.parameters.keys())
        filtered = {k: v for k, v in args.items() if k in valid_params}
        result = tool_fn(**filtered)
        if inspect.isawaitable(result):
            # Safe: this runs in a thread pool thread (no running loop there)
            return asyncio.run(result)
        return result
    except Exception as exc:
        raise exc


async def _execute_subtask(
    subtask: Dict[str, Any],
    msg: OrchestratorMessage,
    tools: Dict[str, Any],
) -> bool:
    """Execute a single subtask. Returns True on success."""
    tool_name = subtask.get("tool", "")
    action = subtask.get("action", "")
    args = subtask.get("args", {}) or {}
    step = subtask.get("step", "?")

    # Resolve command aliases (e.g. "/health" → "get_system_health")
    if tool_name in _COMMAND_TOOL_ALIASES:
        resolved = _COMMAND_TOOL_ALIASES[tool_name]
        print(f"[L5] Alias resolved: {tool_name!r} → {resolved!r}")
        tool_name = resolved

    # Fuzzy fallback: try stripping t_ prefix if tool not found (legacy name guard)
    if tool_name not in tools and tool_name.startswith("t_"):
        stripped = tool_name[2:]
        if stripped in tools:
            print(f"[L5] Legacy t_ prefix stripped: {tool_name!r} → {stripped!r}")
            tool_name = stripped

    # Permission check for trusted tier
    if msg.tier == "trusted" and tool_name in _TRUSTED_BLOCKED:
        err = f"Trusted tier cannot call {tool_name}"
        msg.add_tool_result(tool_name, False, {"error": err})
        print(f"[L5] BLOCKED step={step} {tool_name}: {err}")
        return False

    # Resolve tool
    if tool_name not in tools:
        err = f"Tool {tool_name!r} not found in TOOLS registry"
        msg.add_tool_result(tool_name, False, {"error": err})
        print(f"[L5] UNKNOWN tool={tool_name}  (registry has {len(tools)} tools)")
        return False

    tool_entry = tools[tool_name]
    tool_fn = tool_entry.get("fn") or tool_entry  # TOOLS[name] = {"fn": func, ...} or func directly

    # Smart arg injection for calc: resolve template expressions using prior step results
    if tool_name == "calc" and args.get("expression"):
        expr = str(args["expression"])
        import re as _re
        # If expression contains word-like tokens (template placeholders like {price}, [value], current_x)
        # try to extract a real numeric value from the most recent web_search result
        if _re.search(r'[a-zA-Z_\[\{]{2,}', expr):
            for prev in reversed(msg.tool_results):
                if prev.get("tool") == "web_search" and prev.get("success"):
                    results_text = str(prev.get("result", {}))
                    # Extract first number that looks like a price (4+ digits, handles $71,168.77 format)
                    prices = [x.replace(",", "") for x in _re.findall(r'\$?([\d,]+\.?\d*)', results_text)
                              if len(x.replace(",", "").replace(".", "")) >= 4]
                    if prices:
                        price_str = prices[0].replace(",", "")
                        try:
                            price_val = float(price_str)
                            # Replace ALL non-numeric/operator tokens with the price
                            new_expr = _re.sub(r'[\[\{\(]*[a-zA-Z_][a-zA-Z0-9_\s]*[\]\}\)]*', price_str, expr)
                            # Strip any remaining bracket chars
                            new_expr = _re.sub(r'[\[\]\{\}\(\)]', '', new_expr)
                            new_expr = new_expr.replace(",", "").strip()
                            args = dict(args)
                            args["expression"] = new_expr
                            print(f"[L5] calc smart-inject: {expr!r} → {new_expr!r} (price={price_val})")
                        except ValueError:
                            pass
                    break

    print(f"[L5] step={step}  tool={tool_name}  action={action[:60]!r}")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _call_tool(tool_fn, args)
        )
        success = True
        if isinstance(result, dict):
            success = result.get("ok", True)  # tools return {"ok": bool, ...}
        msg.add_tool_result(tool_name, success, result)
        print(f"[L5] step={step}  {tool_name}  ok={success}")
        return success
    except Exception as exc:
        msg.add_tool_result(tool_name, False, {"error": str(exc)})
        msg.add_error("L5", exc, "TOOL_EXEC_ERROR")
        print(f"[L5] step={step}  {tool_name}  EXCEPTION: {exc}")
        return False


# ── Main layer ────────────────────────────────────────────────────────────────
async def layer_5_tools(msg: OrchestratorMessage):
    """
    Execute all subtasks from msg.plan using real TOOLS registry.
    Direct-response plans skip tool execution entirely.
    """
    msg.track_layer("L5-START")
    plan = msg.plan
    plan_type = plan.get("type", "direct_response")

    if plan_type == "direct_response":
        # No tools needed — direct_answer may already be populated by L4
        print(f"[L5] No tools required (direct_response)")
        msg.track_layer("L5-SKIP")
        from core_orch_layer6 import layer_6_validate
        await layer_6_validate(msg)
        return

    subtasks: List[Dict[str, Any]] = plan.get("subtasks", [])
    if not subtasks:
        print(f"[L5] Empty subtask list")
        msg.track_layer("L5-EMPTY")
        from core_orch_layer6 import layer_6_validate
        await layer_6_validate(msg)
        return

    tools = _get_tools()
    if not tools:
        msg.add_error("L5", Exception("TOOLS registry unavailable"), "TOOLS_UNAVAILABLE")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(msg)
        return

    print(f"[L5] Executing {len(subtasks)} subtask(s) …")

    all_ok = True
    failed_steps = []
    evidence_gate = msg.context.get("evidence_gate", {}) or {}
    evidence_found = False
    for subtask in subtasks:
        ok = await _execute_subtask(subtask, msg, tools)
        if not ok:
            all_ok = False
            failed_steps.append(subtask)
            # GAP-NEW-15: only stop if this subtask is explicitly blocking
            # Default: continue independent steps; stop only if blocking=True
            is_blocking = subtask.get("blocking", False)
            if is_blocking or plan.get("stop_on_failure", False):
                print(f"[L5] Blocking step failed ? stopping pipeline")
                break
            else:
                print(f"[L5] Non-blocking step failed ? continuing")
        else:
            try:
                tool_name = subtask.get("tool", "")
                result = msg.tool_results[-1].get("result") if msg.tool_results else None
                if tool_result_has_evidence(tool_name, result):
                    evidence_found = True
                    stage = subtask.get("evidence_stage")
                    if (
                        evidence_gate.get("needs_retrieval")
                        and not evidence_gate.get("public_research_needed")
                        and (
                            stage == "local_code"
                            or (not evidence_gate.get("repo_map_needed") and stage == "supabase")
                        )
                    ):
                        print(f"[L5] Evidence sufficient after {tool_name}; stopping retrieval sweep early")
                        break
            except Exception as exc:
                print(f"[L5] Evidence gate check non-fatal: {exc}")

    # GAP-NEW-30: if partial failure and multi-step, attempt re-plan
    if failed_steps and not all_ok and plan.get("type") == "multi_step":
        print(f"[L5] Partial failure ({len(failed_steps)} steps) ? triggering re-plan")
        msg.context["failed_steps"] = failed_steps
        msg.context["replan_triggered"] = True
        # Re-plan: strip failed subtasks from plan, let L4 decide next move
        remaining = [s for s in subtasks if s not in failed_steps and not
                     any(r.get("tool") == s.get("tool") for r in msg.tool_results)]
        if remaining:
            msg.plan["subtasks"] = remaining
            msg.plan["_replanned"] = True
            print(f"[L5] Re-plan: {len(remaining)} remaining subtasks")

    # If the evidence sweep found nothing useful, fall back to clarification instead of guessing.
    if evidence_gate.get("needs_retrieval") and not evidence_found and not any(tool_result_has_evidence(r.get("tool", ""), r.get("result")) for r in msg.tool_results):
        msg.response_mode = "clarify"
        msg.context["response_mode"] = "clarify"
        msg.clarification_needed = True
        msg.context["clarification_needed"] = True
        msg.plan = {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low",
            "direct_answer": evidence_gate.get("clarification_prompt")
            or "I could not find enough evidence in CORE memory or the web. Send the file, repo path, URL, or more detail I should verify.",
        }
        print("[L5] Evidence sweep empty — switching to clarification")

    msg.track_layer("L5-COMPLETE")
    print(f"[L5] Execution done: {len(msg.tool_results)} results  all_ok={all_ok}")

    from core_orch_layer6 import layer_6_validate
    await layer_6_validate(msg)
