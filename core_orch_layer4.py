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

    tool_results:   list = []
    tools_called:   list = []
    step                = 0
    start_ts            = time.time()
    task_id             = intent["intent_id"]
    _last_call_sig:str  = ""   # stall detection: track last (tool, args) signature

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
                # Stall detection: same tool + same args twice in a row = infinite loop
                _call_sig = f"{t_name}:{json.dumps(t_args, sort_keys=True, default=str)[:200]}"
                if _call_sig == _last_call_sig:
                    print(f"[L4] STALL detected — {t_name} called twice with same args, breaking")
                    done  = True
                    reply = reply or f"⚠️ Stall detected on tool `{t_name}` — stopped."
                    break
                _last_call_sig = _call_sig

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
        # Only break AFTER tool results are collected so model can synthesize reply
        if done and reply:
            break

        if done and not reply and not tool_calls:
            reply = "✅ Done."
            break

        # If model set done=True but also called tools this step,
        # do one more loop pass so it can synthesize tool results into reply
        if done and tool_calls and not reply:
            done = False   # let loop continue for synthesis pass

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

# Explicit intent → tool mappings (covers cases keyword routing misses)
_INTENT_TOOL_MAP = [
    # Knowledge base queries
    ({"how many kb", "knowledge base", "kb count", "berapa kb"}, ["search_kb", "sb_query"]),
    ({"how many tools", "berapa tools", "list tools"}, ["list_tools", "get_tool_info"]),
    ({"how many task", "task count", "berapa task"}, ["sb_query", "task_health"]),
    ({"mistake", "error log", "what went wrong"}, ["get_mistakes", "search_kb"]),
    ({"pattern", "trading pattern"}, ["search_kb", "sb_query"]),
    ({"status", "health", "are you", "how are you"}, ["get_state", "build_status"]),
    ({"deploy", "redeploy", "restart"}, ["redeploy", "build_status", "railway_logs_live"]),
    ({"patch", "fix file", "edit file"}, ["patch_file", "read_file", "gh_search_replace"]),
    ({"search", "find", "look up", "cari"}, ["search_kb", "web_search"]),
    ({"rule", "behavioral", "aturan"}, ["get_behavioral_rules"]),
    ({"infrastructure", "infra"}, ["get_infrastructure"]),
    ({"evolution", "evolusi"}, ["sb_query"]),
    ({"crypto", "price", "trade", "harga"}, ["crypto_price", "crypto_balance"]),
    ({"weather", "cuaca"}, ["weather"]),
    ({"calculate", "math", "hitung"}, ["calc"]),
    ({"translate", "terjemah"}, ["translate"]),
    ({"write", "create", "buat", "document", "spreadsheet", "presentation"}, 
     ["write_file", "create_document", "create_spreadsheet", "create_presentation"]),
    ({"run", "execute", "python", "script"}, ["run_python"]),
    ({"web", "browse", "fetch", "url"}, ["web_fetch", "web_search", "summarize_url"]),
    ({"github", "repo", "commit", "branch"}, ["read_file", "write_file", "gh_search_replace"]),
    ({"session", "session_start", "session_end"}, ["sb_query", "get_state"]),
    ({"table", "schema", "column"}, ["get_table_schema", "sb_query"]),
]

# Max tools to pass to the model — more than this hurts reasoning quality
_MAX_TOOLS_TO_MODEL = 20


def _select_tools(text: str, hints: list) -> list:
    """
    Return a focused list of tool names relevant to this message.
    Priority: (1) L3 hints, (2) intent map, (3) keyword category routing, (4) always_include.
    Caps at _MAX_TOOLS_TO_MODEL to keep model focused.
    Never returns more than all tools.
    """
    try:
        from core_tools import TOOLS
        from core_config import TOOL_CATEGORY_KEYWORDS, TOOL_ALWAYS_INCLUDE

        all_names   = set(TOOLS.keys())
        always_incl = set(TOOL_ALWAYS_INCLUDE) & all_names
        selected    = set(always_incl)
        tl          = text.lower()

        # (1) L3 hints — fuzzy match: accept partial name matches too
        for h in (hints or []):
            h_lower = h.lower()
            if h in all_names:
                selected.add(h)
            else:
                # partial match e.g. "supabase_query" → "sb_query"
                for name in all_names:
                    if h_lower in name or name in h_lower:
                        selected.add(name)

        # (2) Explicit intent → tool map
        for intent_kws, tool_names in _INTENT_TOOL_MAP:
            if any(kw in tl for kw in intent_kws):
                for t in tool_names:
                    if t in all_names:
                        selected.add(t)

        # (3) Keyword-based category routing (fixed: map category → tool names correctly)
        for cat, kws in TOOL_CATEGORY_KEYWORDS.items():
            if any(kw in tl for kw in kws):
                for name in all_names:
                    # tool name matches any keyword in the triggered category
                    if any(kw in name for kw in kws):
                        selected.add(name)

        result = [t for t in selected if t in all_names]

        # (4) If still empty after all routing — use always_include only (not ALL tools)
        if not result:
            result = list(always_incl) or list(all_names)[:_MAX_TOOLS_TO_MODEL]

        # Cap to avoid flooding model context — prioritise hints + always_include
        if len(result) > _MAX_TOOLS_TO_MODEL:
            priority = list((set(hints or []) | always_incl) & all_names)
            rest     = [t for t in result if t not in priority]
            result   = (priority + rest)[:_MAX_TOOLS_TO_MODEL]

        print(f"[L4] Selected {len(result)} tools for: {text[:60]!r}")
        return result

    except Exception as e:
        print(f"[L4] Tool selection failed: {e}")
        try:
            from core_tools import TOOLS
            from core_config import TOOL_ALWAYS_INCLUDE
            return list(set(TOOL_ALWAYS_INCLUDE) & set(TOOLS.keys()))
        except Exception:
            return []


if __name__ == "__main__":
    print("🛰️ Layer 4: Execution — Online.")
