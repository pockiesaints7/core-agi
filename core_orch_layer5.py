"""
core_orch_layer5.py — L5: Tool Execution
Executes plan subtasks by calling actual tools from core_tools.TOOLS registry.
No simulated results. No mocks.
"""
import asyncio
import inspect
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage

# ── TOOLS registry import ─────────────────────────────────────────────────────
# Lazy import to avoid circular issues at module load time
def _get_tools() -> Dict[str, Any]:
    try:
        from core_tools import TOOLS
        return TOOLS
    except ImportError as exc:
        print(f"[L5] WARNING: core_tools import failed: {exc}")
        return {}


# Tool name aliases to handle /command → t_function mapping
_COMMAND_TOOL_ALIASES = {
    "/health":      "t_health",
    "/state":       "t_state",
    "/status":      "t_state",
    "/tasks":       "t_session_start",
    "/evolutions":  "t_list_evolutions",
    "/kb":          "t_search_kb",
    "/mistakes":    "t_get_mistakes",
    "/train":       "t_trigger_cold_processor",
    "/cold":        "t_trigger_cold_processor",
    "/deploy":      "t_deploy_and_wait",
    "/listen":      "t_listen",
    "/checkpoint":  "t_checkpoint",
}

# Trusted-tier blocked tools (destructive)
_TRUSTED_BLOCKED = frozenset([
    "t_write_file", "t_gh_search_replace", "t_multi_patch",
    "t_core_py_rollback", "t_sb_insert", "t_maintenance_purge",
    "t_railway_env_set", "t_approve_evolution", "t_reject_evolution",
    "t_bulk_reject_evolutions", "t_trigger_cold_processor",
    "t_session_end",
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
        print(f"[L5] UNKNOWN tool={tool_name}")
        return False

    tool_entry = tools[tool_name]
    tool_fn = tool_entry.get("fn") or tool_entry  # TOOLS[name] = {"fn": func, ...} or func directly

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
    for subtask in subtasks:
        ok = await _execute_subtask(subtask, msg, tools)
        if not ok:
            all_ok = False
            # For multi-step plans, stop on first failure unless plan says otherwise
            if plan.get("stop_on_failure", True):
                print(f"[L5] Stopping pipeline after failed step")
                break

    msg.track_layer("L5-COMPLETE")
    print(f"[L5] Execution done: {len(msg.tool_results)} results  all_ok={all_ok}")

    from core_orch_layer6 import layer_6_validate
    await layer_6_validate(msg)
