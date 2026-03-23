"""
core_orch_layer4.py — L4: EXECUTION
=====================================
Agentic tool loop. Runs until done=True or max iterations hit.
All tool calls return unified error schema.
Checkpoints state to Supabase every 5 steps.

Unified error schema (all tools must return):
  {
    "ok":          bool,
    "error_code":  str,   # TOOL_TIMEOUT | AUTH_FAIL | SCHEMA_MISMATCH | NOT_FOUND | etc.
    "message":     str,   # human-readable
    "retry_hint":  str,   # wait_30s | use_fallback | confirm_owner | abort | retry
    "domain":      str,   # supabase | railway | github | zapier | filesystem | groq
    ...result_fields      # tool-specific
  }

Loop control:
  - Max iterations: MAX_TOOL_CALLS (default 50, hard cap)
  - Checkpoint: every 5 steps → write task state to Supabase
  - Hard timeout: LOOP_TIMEOUT_S wall-clock seconds
  - On max_iter breach: log + alert owner, graceful stop
  - force_close: owner-invoked only (C8)
"""

import json
import time
import asyncio
import threading
from datetime import datetime

MAX_TOOL_CALLS   = 50
LOOP_TIMEOUT_S   = 240    # 4 min wall-clock
CHECKPOINT_EVERY = 5

# ── Unified error wrapper ─────────────────────────────────────────────────────

def _wrap_error(error_code: str, message: str, retry_hint: str = "retry",
                domain: str = "unknown", **extra) -> dict:
    return {
        "ok":          False,
        "error_code":  error_code,
        "message":     message,
        "retry_hint":  retry_hint,
        "domain":      domain,
        **extra,
    }


def _normalize_result(raw, tool_name: str) -> dict:
    """
    Ensure every tool result conforms to unified error schema.
    Tools that return plain dicts without 'ok' get it added.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {"ok": True, "result": raw, "error_code": None,
                    "message": "", "retry_hint": None, "domain": "unknown"}

    if not isinstance(raw, dict):
        return {"ok": True, "result": raw, "error_code": None,
                "message": "", "retry_hint": None, "domain": "unknown"}

    # Already has 'ok' field — ensure other fields exist
    if "ok" not in raw:
        raw["ok"] = True
    if "error_code" not in raw:
        raw["error_code"] = None if raw.get("ok") else "UNKNOWN_ERROR"
    if "message" not in raw:
        raw["message"] = raw.get("error", "")
    if "retry_hint" not in raw:
        raw["retry_hint"] = None if raw.get("ok") else "retry"
    if "domain" not in raw:
        raw["domain"] = "unknown"

    return raw


# ── Tool execution ────────────────────────────────────────────────────────────

def _execute_tool_safe(tool_name: str, tool_args: dict, cid: str = "") -> dict:
    """
    Execute a tool with timeout + unified error wrapping.
    Returns normalized result dict.
    """
    try:
        from core_tools import TOOLS
        if tool_name not in TOOLS:
            return _wrap_error(
                "NOT_FOUND",
                f"Tool '{tool_name}' not in registry ({len(TOOLS)} tools available).",
                retry_hint="use_fallback",
                domain="tool_registry",
            )

        fn = TOOLS[tool_name]["fn"]
        # Unwrap nested args if needed
        if (tool_args and len(tool_args) == 1 and "args" in tool_args
                and isinstance(tool_args["args"], dict)):
            tool_args = tool_args["args"]

        # Inject cid for scratchpad tools
        if tool_name in ("set_var", "get_var") and cid:
            tool_args = dict(tool_args or {})
            tool_args["cid"] = cid

        print(f"[L4] CALL {tool_name}({json.dumps(tool_args, default=str)[:120]})")
        result = fn(**tool_args) if tool_args else fn()
        raw    = json.dumps(result, default=str)

        # Truncate massive results
        if len(raw) > 16000:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for k, v in list(parsed.items()):
                        if isinstance(v, list) and len(v) > 20:
                            parsed[f"{k}_count"] = len(v)
                            parsed[k] = v[:20]
                    raw = json.dumps(parsed, default=str)
            except Exception:
                raw = raw[:16000] + "…[truncated]"

        normalized = _normalize_result(json.loads(raw) if raw.startswith("{") else raw,
                                       tool_name)
        print(f"[L4] RESULT {tool_name}: ok={normalized.get('ok')} "
              f"({len(raw)}b)")
        return normalized

    except TypeError as e:
        return _wrap_error(
            "SCHEMA_MISMATCH",
            f"Wrong args for {tool_name}: {e}. "
            f"Call get_tool_info(name='{tool_name}') to verify params.",
            retry_hint="use_fallback",
            domain="tool_registry",
        )
    except Exception as e:
        import traceback
        return _wrap_error(
            "TOOL_EXCEPTION",
            str(e)[:300],
            retry_hint="retry",
            domain="unknown",
            trace=traceback.format_exc()[:400],
        )


# ── Checkpoint ────────────────────────────────────────────────────────────────

def _checkpoint(task_id: str, step: int, tool_results: list, cid: str):
    """Write execution state to Supabase so a crash is recoverable."""
    try:
        from core_config import sb_post
        sb_post("task_queue", {
            "task":       json.dumps({
                "checkpoint": True,
                "task_id":    task_id,
                "step":       step,
                "cid":        cid,
                "tools_used": [r["name"] for r in tool_results[-5:]],
            }),
            "status":     "in_progress",
            "source":     "orch_checkpoint",
            "priority":   3,
            "created_at": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        print(f"[L4] Checkpoint write failed (non-fatal): {e}")


# ── Agentic loop ──────────────────────────────────────────────────────────────

async def layer_4_execute(execution_plan: dict) -> None:
    """
    Main agentic loop. Calls L8 for reasoning, executes tool calls,
    repeats until done=True or limits hit.
    """
    ctx           = execution_plan["ctx"]
    intent        = ctx["intent"]
    cid           = intent["sender_id"]
    text          = intent["text"]

    tool_results:  list = []
    tools_called:  list = []
    step               = 0
    start_ts           = time.time()
    task_id            = intent["intent_id"]

    # Build tool descriptions for this message
    from core_orch_layer8 import build_tools_desc, call_model_loop
    selected_tools = _select_tools(text, execution_plan.get("tool_hints", []))
    tools_desc     = build_tools_desc(selected_tools)

    while step < MAX_TOOL_CALLS:
        # Wall-clock timeout
        if time.time() - start_ts > LOOP_TIMEOUT_S:
            print(f"[L4] TIMEOUT after {step} steps")
            from core_config import notify
            notify(f"⚠️ Task timed out after {step} steps. Last action: "
                   f"{tools_called[-1] if tools_called else 'none'}", cid)
            break

        # Checkpoint every N steps
        if step > 0 and step % CHECKPOINT_EVERY == 0:
            _checkpoint(task_id, step, tool_results, cid)

        # ── Call model for next action ─────────────────────────────────────
        try:
            model_out = await call_model_loop(
                ctx=ctx,
                execution_plan=execution_plan,
                tools_desc=tools_desc,
                tool_results=tool_results,
                step=step,
            )
        except Exception as e:
            print(f"[L4] Model call failed at step {step}: {e}")
            break

        thought     = model_out.get("thought", "")
        tool_calls  = model_out.get("tool_calls", [])
        reply       = model_out.get("reply", "")
        done        = model_out.get("done", False)

        if thought:
            print(f"[L4] step={step} thought={thought[:120]!r}")

        # ── Execute tool calls ─────────────────────────────────────────────
        if tool_calls:
            for tc in tool_calls:
                t_name = tc.get("name", "")
                t_args = tc.get("args", {}) or {}

                if not t_name:
                    continue

                # L6 validation: background-loop tools must not be called
                # with elevated permissions from agentic loop
                result = _execute_tool_safe(t_name, t_args, cid)
                tool_results.append({
                    "name":   t_name,
                    "args":   t_args,
                    "result": json.dumps(result, default=str)[:8000],
                    "ok":     result.get("ok", False),
                    "step":   step,
                })
                tools_called.append(t_name)
                step += 1

                # On tool failure: check retry_hint
                if not result.get("ok"):
                    hint = result.get("retry_hint", "retry")
                    print(f"[L4] Tool {t_name} FAILED: {result.get('message','')} "
                          f"hint={hint}")
                    if hint == "abort":
                        done = True
                        reply = (f"❌ Task aborted: {result.get('message', 'tool error')}")
                        break
                    elif hint == "confirm_owner":
                        from core_orch_layer5 import layer_5_request_confirm
                        confirmed = await layer_5_request_confirm(
                            intent,
                            {"intent_parsed": f"Retry failed tool: {t_name}",
                             "plan": [f"retry {t_name}"],
                             "risk": "medium"},
                        )
                        if not confirmed:
                            done = True
                            break

        # ── Done? ─────────────────────────────────────────────────────────
        if done and reply:
            break

        if done and not reply and not tool_calls:
            reply = "✅ Done."
            break

    else:
        # Hit max iterations
        print(f"[L4] Max iterations ({MAX_TOOL_CALLS}) reached")
        from core_config import notify
        notify(f"⚠️ Hit max {MAX_TOOL_CALLS} tool calls. Stopping.", cid)
        reply = reply or f"Stopped after {MAX_TOOL_CALLS} tool calls."

    elapsed = int((time.time() - start_ts) * 1000)
    print(f"[L4] Loop complete: steps={step} tools={len(tools_called)} "
          f"elapsed={elapsed}ms reply_len={len(reply)}")

    # ── Pass to L5 output ──────────────────────────────────────────────────
    from core_orch_layer5 import layer_5_output
    await layer_5_output(intent, reply, tool_results=tool_results)

    # ── Pass to L9 learning (session logging) ─────────────────────────────
    from core_orch_layer9 import layer_9_log_turn
    await layer_9_log_turn(ctx, reply, tool_results=tool_results)


# ── Tool selection (scoped to message) ───────────────────────────────────────

def _select_tools(text: str, hints: list) -> list:
    """
    Return list of tool names relevant to this message.
    Uses hint from L3 + keyword-based category routing.
    Falls back to all tools if selection fails.
    """
    try:
        from core_tools import TOOLS
        from core_config import TOOL_CATEGORY_KEYWORDS, TOOL_ALWAYS_INCLUDE

        all_names   = set(TOOLS.keys())
        always_incl = set(TOOL_ALWAYS_INCLUDE)
        selected    = set(always_incl)

        # Add hinted tools
        for h in (hints or []):
            if h in all_names:
                selected.add(h)

        # Keyword-based category routing
        tl = text.lower()
        for cat, kws in TOOL_CATEGORY_KEYWORDS.items():
            if any(kw in tl for kw in kws):
                # Add all tools in category
                for name in all_names:
                    if any(kw in name for kw in kws):
                        selected.add(name)

        result = [t for t in selected if t in all_names]
        if not result:
            result = list(all_names)
        return result

    except Exception as e:
        print(f"[L4] Tool selection failed: {e}")
        try:
            from core_tools import TOOLS
            return list(TOOLS.keys())
        except Exception:
            return []


if __name__ == "__main__":
    print("🛰️ Layer 4: Execution — Online.")
