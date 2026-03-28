"""
core_orch_layer6.py — L6: Validation & Verification
Checks tool outputs for structural correctness and failures.
GAP-NEW-16: semantic validation — ok=True with status=degraded flagged as warning
GAP-NEW-17: fatal errors vs warnings split — separate paths through pipeline
"""
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage

# Error codes that indicate a fatal (unrecoverable) condition
_FATAL_CODES = frozenset([
    "PREFLIGHT_BLOCKED", "AUTH_FAILED", "QUOTA_EXCEEDED",
    "IMPORT_ERROR", "SCHEMA_MISMATCH", "PERMISSION_DENIED",
])

# Statuses that count as degraded even when ok=True (GAP-NEW-16)
_DEGRADED_STATUSES = frozenset(["degraded", "partial", "timeout", "error", "stale"])


def _classify_error(err_msg: str, error_code: str) -> str:
    """Return 'fatal' or 'warning' for a given error."""
    if error_code in _FATAL_CODES:
        return "fatal"
    low = err_msg.lower()
    if any(w in low for w in ("crash", "unrecoverable", "auth", "permission", "quota")):
        return "fatal"
    return "warning"


def _check_semantic(tool: str, result: Any) -> List[str]:
    """
    GAP-NEW-16: semantic validation.
    Catches ok=True but status=degraded/partial/timeout — surfaces as warning.
    """
    warnings = []
    if not isinstance(result, dict):
        return warnings

    # ok=True but status signals degraded
    if result.get("ok") is True:
        status = str(result.get("status", "")).lower()
        if status in _DEGRADED_STATUSES:
            warnings.append(f"{tool}: ok=True but status={status} (degraded)")

    # health-style: overall not ok
    overall = str(result.get("overall", "")).lower()
    if overall and overall not in ("ok", "healthy", ""):
        warnings.append(f"{tool}: health overall={overall}")

    # Components with non-ok sub-status
    components = result.get("components", {})
    if isinstance(components, dict):
        bad = [f"{k}={v}" for k, v in components.items()
               if str(v).lower() not in ("ok", "healthy", "")]
        if bad:
            warnings.append(f"{tool}: degraded components: {', '.join(bad[:4])}")

    return warnings


async def layer_6_validate(msg: OrchestratorMessage):
    """
    Inspect tool results. Surface failures clearly.
    Fatal errors halt pipeline to output. Warnings are noted but continue.
    """
    msg.track_layer("L6-START")

    total = len(msg.tool_results)
    fatal_errors = []
    warning_errors = []
    semantic_warnings = []
    output_validation_packets = []

    for r in msg.tool_results:
        tool_name = r.get("tool", "?")
        result_data = r.get("result", {})

        # --- Tool-level failure ---
        if not r.get("success", True):
            err_msg = (
                result_data.get("error", "")
                or result_data.get("message", "")
                or "Tool returned ok=False"
            ) if isinstance(result_data, dict) else str(result_data)
            error_code = result_data.get("error_code", "TOOL_FAILED") if isinstance(result_data, dict) else "TOOL_FAILED"
            severity = _classify_error(err_msg, error_code)
            if severity == "fatal":
                fatal_errors.append((tool_name, err_msg, error_code))
                msg.add_error("L6", Exception(err_msg), f"FATAL:{error_code}:{tool_name}")
            else:
                warning_errors.append((tool_name, err_msg, error_code))
                msg.add_error("L6", Exception(err_msg), f"WARNING:{error_code}:{tool_name}")

        # --- Semantic validation (ok=True but degraded) ---
        sem_warns = _check_semantic(tool_name, result_data)
        semantic_warnings.extend(sem_warns)

        # --- Structural tool-output validation ---
        try:
            from core_tools import _validate_tool_output_packet

            output_validation = _validate_tool_output_packet(
                tool_name,
                result_data,
                success=r.get("success", True),
            )
            output_validation_packets.append(output_validation)
            if output_validation.get("fatal"):
                fatal_errors.append((tool_name, output_validation.get("summary", "invalid tool output"), "TOOL_OUTPUT_INVALID"))
                msg.add_error("L6", Exception(output_validation.get("summary", "invalid tool output")), f"FATAL:TOOL_OUTPUT_INVALID:{tool_name}")
            elif output_validation.get("warnings"):
                warning_errors.append((tool_name, output_validation.get("summary", "tool output warning"), "TOOL_OUTPUT_WARNING"))
                msg.add_error("L6", Exception(output_validation.get("summary", "tool output warning")), f"WARNING:TOOL_OUTPUT_WARNING:{tool_name}")
        except Exception as exc:
            fatal_errors.append((tool_name, str(exc), "TOOL_OUTPUT_VALIDATION_ERROR"))
            msg.add_error("L6", Exception(str(exc)), f"FATAL:TOOL_OUTPUT_VALIDATION_ERROR:{tool_name}")

    # Store semantic warnings in context for L9 to reference
    if semantic_warnings:
        msg.context["semantic_warnings"] = semantic_warnings
        print(f"[L6] Semantic warnings: {semantic_warnings}")
    if output_validation_packets:
        msg.context["tool_output_validation"] = output_validation_packets
        print(f"[L6] Tool output validation: {[p.get('summary', '?') for p in output_validation_packets[:6]]}")

    passed = len(fatal_errors) == 0
    msg.validation_status = {
        "passed": passed,
        "total": total,
        "ok": total - len(fatal_errors) - len(warning_errors),
        "warnings": len(warning_errors) + len(semantic_warnings),
        "fatal": len(fatal_errors),
        "tool_output_validations": len(output_validation_packets),
    }

    msg.track_layer("L6-COMPLETE")
    print(
        f"[L6] Validation: total={total}  fatal={len(fatal_errors)}"
        f"  warnings={len(warning_errors)}  semantic_warns={len(semantic_warnings)}"
    )

    # Fatal errors → skip to output immediately
    if not passed:
        print(f"[L6] FATAL errors detected — routing to L10 directly")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(msg)
        return

    from core_orch_layer7 import layer_7_refine
    await layer_7_refine(msg)
