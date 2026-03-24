"""
core_orch_layer6.py — L6: Validation & Verification
Checks tool outputs for structural correctness and failures.
"""
import json
from typing import Any, Dict

from orchestrator_message import OrchestratorMessage


async def layer_6_validate(msg: OrchestratorMessage):
    """
    Inspect tool results. Surface failures clearly.
    Non-blocking — failed tools are noted but pipeline continues to output.
    """
    msg.track_layer("L6-START")

    total = len(msg.tool_results)
    failed = [r for r in msg.tool_results if not r.get("success", True)]
    passed = total - len(failed)

    # Surface any tool-level error messages into msg.errors
    for r in failed:
        result_data = r.get("result", {})
        err_msg = (
            result_data.get("error", "")
            or result_data.get("message", "")
            or "Tool returned ok=False"
        ) if isinstance(result_data, dict) else str(result_data)
        msg.add_error("L6", Exception(err_msg), f"TOOL_FAILED:{r.get('tool','?')}")

    msg.validation_status = {
        "passed": len(failed) == 0,
        "total": total,
        "ok": passed,
        "failed": len(failed),
    }

    msg.track_layer("L6-COMPLETE")
    print(f"[L6] Validation: total={total}  ok={passed}  failed={len(failed)}")

    from core_orch_layer7 import layer_7_refine
    await layer_7_refine(msg)
