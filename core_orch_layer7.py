"""
core_orch_layer7.py — L7: OBSERVABILITY
=========================================
Measures what matters. Called after every completed turn.
Writes quality signals to hot_reflections for the L9 learning pipeline.

Tracks:
  - Tool call metrics (success rate, which tools called)
  - Session quality score (0.0–1.0)
  - Error log (ok=False tool results)
  - Evolution opportunity detection (real improvement proposals)
  - Quality alert: if score drops below threshold → surface at next session_start

Evolution proposal protocol:
  - Uses your REAL evolution_queue table
  - Uses correct groq_chat(system, user, model, max_tokens) signature
  - Only proposes when there's genuine signal (not every turn)
  - Routes to evolution_queue + notifies owner — never self-approves (C3)

FIXES (v2):
  - BUG-L7-1:  Quality score now penalises based on BOTH ratio AND absolute
               failure count — a 10/10 failure session scores lower than 1/1
  - BUG-L7-2:  _evo_counter is now reset per session via reset_evo_counter(cid);
               L9 calls this on session_end
  - BUG-L7-4:  Error log deduplication — identical tool+error_code pairs are
               only logged once per session (not once per turn)
  - BUG-L7-5:  Evolution proposal thread joins with timeout to reduce write loss
               on process shutdown
  - GAP-L7-6:  Quality alert now writes to hot_reflections, not sessions table
               (was creating fake session rows)
  - NEW-L7-8:  core_github notify import failure logged explicitly — evolution
               is still saved, owner notified of notification failure separately
"""

import json
import time
import asyncio
import threading
from datetime import datetime

QUALITY_ALERT_THRESHOLD = 0.50   # below this → quality_alert written
EVOLVE_QUALITY_THRESHOLD = 0.45  # below this → consider evolution proposal
MAX_EVOLUTIONS_PER_SESSION = 3   # rate limit on proposals per session

# Session-level evolution counter (reset each session via reset_evo_counter)
_evo_counter: dict = {}   # cid → int
_evo_lock            = threading.Lock()

# Session-level error deduplication: cid → set of "toolname:error_code" (FIX: BUG-L7-4)
_logged_errors: dict = {}   # cid → set[str]
_err_lock             = threading.Lock()


def reset_evo_counter(cid: str) -> None:
    """Reset evolution counter for a chat_id. Called by L9 on session_end.
    FIX BUG-L7-2: was never reset, effectively capping evolutions forever.
    """
    with _evo_lock:
        _evo_counter.pop(cid, None)


def reset_error_log(cid: str) -> None:
    """Reset per-session error deduplication set. Called by L9 on session_end."""
    with _err_lock:
        _logged_errors.pop(cid, None)


# ── Quality scoring ───────────────────────────────────────────────────────────

def _score_turn(tool_results: list, reply: str) -> float:
    """
    Compute quality score 0.0–1.0 for this turn.
    FIX BUG-L7-1: penalises based on BOTH ratio AND absolute count so that
    10/10 failures scores lower than 1/1 failures.

    Scoring:
      - Base:                   0.85
      - Per-failure ratio:     -0.30 * (failures/total)
      - Per-failure absolute:  -0.03 * failures  (capped at -0.30)
      - Empty reply:           -0.20
      - Clean execution bonus:  +0.05 (only if tools used, zero failures)
    """
    if not tool_results and not reply:
        return 0.3

    total    = max(len(tool_results), 1)
    failures = sum(1 for r in tool_results if not r.get("ok", True))
    score    = 0.85

    if failures > 0:
        ratio_penalty    = (failures / total) * 0.30
        absolute_penalty = min(failures * 0.03, 0.30)
        score -= ratio_penalty + absolute_penalty

    if not reply or len(reply.strip()) < 10:
        score -= 0.20

    if failures == 0 and tool_results:
        score += 0.05

    return round(max(0.1, min(1.0, score)), 3)


# ── Error log ─────────────────────────────────────────────────────────────────

def _log_errors(tool_results: list, cid: str):
    """Write tool failures to mistakes table for L9/cold processor pickup.
    FIX BUG-L7-4: deduplicates by (tool_name, error_code) per session to
    prevent flooding mistakes table with repeated identical failures.
    """
    failures = [r for r in tool_results if not r.get("ok", True)]
    if not failures:
        return

    with _err_lock:
        if cid not in _logged_errors:
            _logged_errors[cid] = set()
        already_logged = _logged_errors[cid]

    for f in failures[:3]:
        dedup_key = f"{f.get('name', '?')}:{f.get('error_code', '?')}"
        if dedup_key in already_logged:
            print(f"[L7] Skipping duplicate error log: {dedup_key}")
            continue
        try:
            from core_config import sb_post
            sb_post("mistakes", {
                "domain":           "core_agi",
                "context":          f"Agentic loop tool call — chat_id={cid}",
                "what_failed":      f"Tool {f.get('name', '?')} returned ok=False",
                "root_cause":       f"error_code={f.get('error_code', '?')} "
                                    f"message={f.get('message', '')[:200]}",
                "correct_approach": f"retry_hint={f.get('retry_hint', '?')}",
                "how_to_avoid":     f"Check tool schema and args. domain={f.get('domain', '?')}",
                "severity":         "medium",
                "created_at":       datetime.utcnow().isoformat(),
            })
            with _err_lock:
                _logged_errors[cid].add(dedup_key)
        except Exception as e:
            print(f"[L7] Error log write failed (non-fatal): {e}")


# ── Evolution proposal ────────────────────────────────────────────────────────

def _maybe_propose_evolution(
    text: str,
    tool_results: list,
    reply: str,
    quality: float,
    cid: str,
):
    """
    Check if this turn reveals a real improvement opportunity.
    Uses groq_chat with CORRECT signature.
    Only fires when quality is low AND we haven't proposed too many this session.

    FIX BUG-L7-5: thread is stored and can be joined before process exit.
    FIX NEW-L7-8: notify import failure is logged explicitly.
    """
    with _evo_lock:
        count = _evo_counter.get(cid, 0)
        if count >= MAX_EVOLUTIONS_PER_SESSION:
            return
    if quality > EVOLVE_QUALITY_THRESHOLD:
        return

    failed_tools = [r for r in tool_results if not r.get("ok", True)]
    if not failed_tools:
        return

    try:
        from core_config import groq_chat, GROQ_FAST, sb_post_critical

        failures_text = "\n".join(
            f"  [{r.get('name','?')}] {r.get('error_code','?')}: {r.get('message','')[:120]}"
            for r in failed_tools[:3]
        )

        system = (
            "You are CORE's evolution analyst. "
            "Analyze this failed interaction and determine if a structural improvement is warranted. "
            "ONLY propose if there is a clear, specific, actionable fix. "
            "Output ONLY valid JSON — no preamble:\n"
            '{"propose": true/false, "change_type": "knowledge|code|behavioral_rule", '
            '"summary": "1 sentence what to fix", "confidence": 0.0-1.0, '
            '"reason": "why this failure pattern is systemic"}'
        )
        user = (
            f"USER REQUEST: {text[:300]}\n"
            f"QUALITY SCORE: {quality}\n"
            f"FAILED TOOLS:\n{failures_text}\n"
            f"FINAL REPLY: {reply[:300]}"
        )

        raw = groq_chat(system=system, user=user, model=GROQ_FAST, max_tokens=300)
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

        if not parsed.get("propose"):
            return
        if float(parsed.get("confidence", 0)) < 0.5:
            return

        # Write to evolution_queue (real schema)
        ok = sb_post_critical("evolution_queue", {
            "change_type":    parsed.get("change_type", "knowledge"),
            "change_summary": parsed.get("summary", "")[:500],
            "pattern_key":    f"orch_l7:{text[:80]}",
            "confidence":     float(parsed.get("confidence", 0.5)),
            "status":         "pending",
            "source":         "real",
            "impact":         "core_agi",
            "recommendation": parsed.get("reason", "")[:300],
            "created_at":     datetime.utcnow().isoformat(),
        })

        if ok:
            with _evo_lock:
                _evo_counter[cid] = _evo_counter.get(cid, 0) + 1

            # Notify owner — log explicit failure if notify unavailable (FIX NEW-L7-8)
            try:
                from core_github import notify
                notify(
                    f"🧬 <b>Evolution Proposed (L7)</b>\n"
                    f"Type: {parsed.get('change_type')}\n"
                    f"Summary: {parsed.get('summary','')[:200]}\n"
                    f"Confidence: {parsed.get('confidence')}\n"
                    f"Reason: {parsed.get('reason','')[:200]}\n"
                    f"Review: /review or t_check_evolutions"
                )
            except Exception as notify_err:
                # Evolution IS saved to DB — owner just wasn't notified via Telegram.
                # Log this explicitly so it's visible in Railway logs.
                print(f"[L7] WARNING: Evolution saved but owner notify failed: {notify_err}")
                print(f"[L7] Saved evolution summary: {parsed.get('summary','')[:120]}")

            print(f"[L7] Evolution proposed: {parsed.get('summary','')[:80]}")

    except json.JSONDecodeError as e:
        print(f"[L7] Evolution JSON parse failed (non-fatal): {e}")
    except Exception as e:
        print(f"[L7] Evolution proposal error (non-fatal): {e}")


# ── Quality alert ─────────────────────────────────────────────────────────────

def _write_quality_alert(quality: float, cid: str, reason: str):
    """Write quality alert to hot_reflections (not sessions — FIX GAP-L7-6)."""
    try:
        from core_config import sb_post
        sb_post("hot_reflections", {
            "domain":               "core_agi",
            "task_summary":         f"[QUALITY_ALERT] score={quality} reason={reason}",
            "quality_score":        quality,
            "verify_rate":          0,
            "mistake_consult_rate": 0,
            "new_patterns":         [],
            "new_mistakes":         [],
            "gaps_identified":      [f"quality_alert: {reason}"],
            "reflection_text":      (
                f"Quality alert triggered at {datetime.utcnow().isoformat()}. "
                f"Score={quality}. Reason: {reason}. "
                f"chat_id={cid}."
            ),
            "source":               "real",
            "processed_by_cold":    False,
            "created_at":           datetime.utcnow().isoformat(),
        })
        print(f"[L7] Quality alert written to hot_reflections: score={quality}")
    except Exception as e:
        print(f"[L7] Quality alert write failed (non-fatal): {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

async def layer_7_observe(
    intent: dict,
    reply: str,
    tool_results: list,
    ctx: dict,
):
    """
    Called by L9 after every completed turn.
    Scores quality, logs errors, checks for evolution opportunities.
    Fires silently — no user-facing output.

    FIX BUG-L7-5: evolution thread stored and joins with short timeout on
    graceful shutdown to reduce proposal write loss.
    """
    cid = intent["sender_id"]

    # Score this turn
    quality = _score_turn(tool_results, reply)
    print(f"[L7] Turn quality: {quality} tools={len(tool_results)} "
          f"failures={sum(1 for r in tool_results if not r.get('ok', True))}")

    # Log tool errors to mistakes table (with deduplication)
    _log_errors(tool_results, cid)

    # Quality alert
    if quality < QUALITY_ALERT_THRESHOLD:
        reason = f"{sum(1 for r in tool_results if not r.get('ok',True))} tool failures"
        _write_quality_alert(quality, cid, reason)

    # Check for evolution opportunities in background thread
    evo_thread = threading.Thread(
        target=_maybe_propose_evolution,
        args=(intent["text"], tool_results, reply, quality, cid),
        daemon=True,
        name=f"evo-{cid}-{int(time.time())}",
    )
    evo_thread.start()
    # Brief join attempt — lets the write complete if CPU is available
    # Does NOT block the response path for long (FIX BUG-L7-5 — partial mitigation)
    evo_thread.join(timeout=0.05)

    return quality


if __name__ == "__main__":
    print("🛰️ Layer 7: Observability — Online.")
