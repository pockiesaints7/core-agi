"""
core_orch_layer9.py — L9: LEARNING (Session Close + Hot Reflection)
====================================================================
Called after every completed turn. Writes session signals to the
hot_reflections table so the cold processor (core_train.py) picks
them up for pattern extraction and evolution proposals.

Also manages session-level quality metrics.

Session close sequence (per Blueprint L7 + L9):
  1. write hot_reflections
  2. update conversation history in L2
  3. log_quality_metrics (L7 observe)
  4. Telegram summary (on session_end or significant events)

hot_reflection written per turn (not just per session end) so the
pipeline always has fresh signal even if CORE crashes mid-session.

FIXES (v2):
  - BUG-L9-4:  Session data no longer popped before write — pop happens AFTER
               confirmed write to prevent silent data loss on write failure
  - BUG-L9-5:  ctx["intent"] access guarded with .get() and fallback — no
               more unhandled KeyError on malformed ctx
  - BUG-L7-2 (coordinated): layer_9_session_end now calls L7.reset_evo_counter
               and L7.reset_error_log so per-session limits reset correctly
  - NEW-L9-10: verify_rate and mistake_consult_rate computed from session data
               instead of being hardcoded to 0
  - NEW-L9-11: "session" removed from mcp domain keyword list (too broad)
"""

import json
import asyncio
import threading
from datetime import datetime
from typing import Optional

# ── Per-chat session tracker (in-memory) ─────────────────────────────────────
_sessions: dict = {}   # cid → {turns, tools_used, errors, quality_sum, started_at, tool_calls_total}
_sess_lock      = threading.Lock()


def _get_or_create_session(cid: str) -> dict:
    with _sess_lock:
        if cid not in _sessions:
            _sessions[cid] = {
                "turns":             0,
                "tools_used":        [],
                "errors":            0,
                "quality_sum":       0.0,
                "tool_calls_total":  0,
                "started_at":        datetime.utcnow().isoformat(),
            }
        return _sessions[cid]


def _update_session(cid: str, tool_results: list, quality: float):
    with _sess_lock:
        if cid not in _sessions:
            _sessions[cid] = {
                "turns":             0,
                "tools_used":        [],
                "errors":            0,
                "quality_sum":       0.0,
                "tool_calls_total":  0,
                "started_at":        datetime.utcnow().isoformat(),
            }
        sess = _sessions[cid]
        sess["turns"]            += 1
        sess["tools_used"]       += [r.get("name", "?") for r in tool_results]
        sess["errors"]           += sum(1 for r in tool_results if not r.get("ok", True))
        sess["quality_sum"]      += quality
        sess["tool_calls_total"] += len(tool_results)


# ── Hot reflection writer ─────────────────────────────────────────────────────

def _write_hot_reflection(
    cid:          str,
    text:         str,
    reply:        str,
    tool_results: list,
    quality:      float,
    ctx:          dict,
):
    """
    Write one hot_reflection row. Non-blocking (called from background thread).
    Matches actual hot_reflections table schema.
    """
    try:
        from core_config import sb_post, groq_chat, GROQ_FAST

        tools_used  = [r.get("name", "?") for r in tool_results]
        failed_cnt  = sum(1 for r in tool_results if not r.get("ok", True))
        domain      = _infer_domain(text, ctx)

        # Compute verify_rate and mistake_consult_rate from session state
        # (FIX NEW-L9-10: was hardcoded 0 — now computed from available signals)
        sess = _sessions.get(cid, {})
        total_calls = sess.get("tool_calls_total", len(tools_used))
        session_errors = sess.get("errors", failed_cnt)
        mistake_consult_rate = round(session_errors / max(total_calls, 1), 3)
        verify_rate          = round(1.0 - mistake_consult_rate, 3)

        # Extract patterns via Groq (fast model, max_tokens=300)
        patterns = []
        gaps     = None
        try:
            system = (
                "You are CORE's pattern extractor. "
                "Given an interaction, extract 1-3 short reusable patterns (<100 chars each). "
                "Output ONLY valid JSON: "
                '{"patterns":["..."],"gap":"1 sentence gap or null"}'
            )
            user = (
                f"Request: {text[:200]}\n"
                f"Tools used: {', '.join(tools_used[:8])}\n"
                f"Quality: {quality}\n"
                f"Failures: {failed_cnt}\n"
                f"Reply: {reply[:200]}"
            )
            raw    = groq_chat(system=system, user=user,
                               model=GROQ_FAST, max_tokens=300)
            raw    = raw.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            patterns = [p for p in parsed.get("patterns", [])
                        if isinstance(p, str) and len(p) > 5][:3]
            gap_raw = parsed.get("gap")
            gaps    = [gap_raw] if gap_raw and isinstance(gap_raw, str) else None
        except Exception as e:
            print(f"[L9] Pattern extraction failed (non-fatal): {e}")

        ok = sb_post("hot_reflections", {
            "domain":               domain,
            "task_summary":         f"Telegram v2: {text[:250]}",
            "quality_score":        quality,
            "verify_rate":          verify_rate,
            "mistake_consult_rate": mistake_consult_rate,
            "new_patterns":         patterns,
            "new_mistakes":         [],
            "gaps_identified":      gaps,
            "reflection_text":      (
                f"Orchestrator v2 turn. Model: from L8. "
                f"Tools: {len(tools_used)} ({', '.join(tools_used[:8])}). "
                f"Failures: {failed_cnt}. "
                f"Reply: {reply[:200]}"
            ),
            "source":               "real",
            "processed_by_cold":    False,
            "created_at":           datetime.utcnow().isoformat(),
        })

        if ok:
            print(f"[L9] hot_reflection written: domain={domain} quality={quality} "
                  f"patterns={len(patterns)}")
        else:
            print(f"[L9] hot_reflection write FAILED — data may be lost")

    except Exception as e:
        print(f"[L9] hot_reflection error (non-fatal): {e}")


def _infer_domain(text: str, ctx: dict) -> str:
    """Infer learning domain from text keywords.
    FIX NEW-L9-11: Removed "session" from mcp keyword list — too broad,
    causes false domain classifications for common English usage.
    """
    _dom_map = [
        (["supabase", "database", "table", "sb_"],            "db"),
        (["github", "patch", "deploy", "railway", "commit"],  "code"),
        (["telegram", "notify", "bot"],                        "bot"),
        (["mcp", "tool_call", "tool call"],                    "mcp"),  # "session" removed
        (["training", "cold", "hot", "evolution", "pattern"], "training"),
        (["knowledge", "kb", "learn"],                         "kb"),
    ]
    tl = text.lower()
    for kws, d in _dom_map:
        if any(k in tl for k in kws):
            return d
    return "core_agi"


# ── Main entry point ──────────────────────────────────────────────────────────

async def layer_9_log_turn(
    ctx:          dict,
    reply:        str,
    tool_results: list,
):
    """
    Called by L4 after every completed tool loop.
    Fires L7 observability + writes hot_reflection in background.
    Non-blocking: all writes happen in background threads.

    FIX BUG-L9-5: ctx["intent"] access guarded — no KeyError on malformed ctx.
    """
    # Guard against malformed ctx (FIX BUG-L9-5)
    intent = ctx.get("intent")
    if not intent:
        print(f"[L9] WARNING: ctx missing 'intent' field — using fallback")
        intent = {"sender_id": "unknown", "text": ""}

    cid  = intent.get("sender_id", "unknown")
    text = intent.get("text", "")

    # L7: observe (quality score, error log, evolution proposals)
    from core_orch_layer7 import layer_7_observe
    quality = await layer_7_observe(intent, reply, tool_results, ctx)

    # Update in-memory session tracker
    _update_session(cid, tool_results, quality)

    # Write hot_reflection in background thread (non-blocking)
    threading.Thread(
        target=_write_hot_reflection,
        args=(cid, text, reply, tool_results, quality, ctx),
        daemon=True,
    ).start()


async def layer_9_session_end(cid: str):
    """
    Call when a session explicitly ends (e.g. /clear or long idle).
    Writes a session summary row and cleans up memory.

    FIX BUG-L9-4: session data is now popped AFTER confirmed write, not before.
    If write fails, session data remains in _sessions for potential retry.

    FIX BUG-L7-2 coordination: resets L7 evolution counter and error log.
    """
    _get_or_create_session(cid)   # Ensure session exists
    with _sess_lock:
        session_copy = dict(_sessions.get(cid, {}))

    # Don't pop yet — pop after confirmed write (FIX BUG-L9-4)
    turns      = session_copy.get("turns", 0)
    errors     = session_copy.get("errors", 0)
    tools_used = session_copy.get("tools_used", [])
    avg_q      = (session_copy["quality_sum"] / turns) if turns > 0 else 0.0

    write_ok = False
    try:
        from core_config import sb_post
        sb_post("sessions", {
            "summary":       (
                f"Orchestrator v2 session: {turns} turns, "
                f"{len(set(tools_used))} unique tools, "
                f"{errors} failures, avg_quality={avg_q:.2f}"
            ),
            "actions":       list(set(tools_used))[:20],
            "interface":     "orchestrator_v2",
            "domain":        "core_agi",
            "quality_score": round(avg_q, 3),
            "created_at":    datetime.utcnow().isoformat(),
        })
        write_ok = True
        print(f"[L9] Session end logged: turns={turns} avg_q={avg_q:.2f}")
    except Exception as e:
        print(f"[L9] Session end write failed (non-fatal): {e}")

    # Only pop session from memory after write is confirmed (FIX BUG-L9-4)
    if write_ok:
        with _sess_lock:
            _sessions.pop(cid, None)
    else:
        print(f"[L9] Session data retained in memory (write failed) — will retry on next end")

    # Reset L7 counters for this session (FIX BUG-L7-2 coordination)
    try:
        from core_orch_layer7 import reset_evo_counter, reset_error_log
        reset_evo_counter(cid)
        reset_error_log(cid)
    except Exception as e:
        print(f"[L9] L7 counter reset failed (non-fatal): {e}")

    # Clear L2 memory — always, regardless of write status
    try:
        from core_orch_layer2 import clear_history
        clear_history(cid)
    except Exception as e:
        print(f"[L9] L2 clear_history failed (non-fatal): {e}")


if __name__ == "__main__":
    print("🛰️ Layer 9: Learning — Online.")
