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
"""

import json
import time
import asyncio
from datetime import datetime

QUALITY_ALERT_THRESHOLD = 0.50   # below this → quality_alert row written
EVOLVE_QUALITY_THRESHOLD = 0.45  # below this → consider evolution proposal
MAX_EVOLUTIONS_PER_SESSION = 3   # rate limit on proposals

# Session-level evolution counter (reset each "session" = per conversation)
_evo_counter: dict = {}   # cid → int


# ── Quality scoring ───────────────────────────────────────────────────────────

def _score_turn(tool_results: list, reply: str) -> float:
    """
    Compute quality score 0.0–1.0 for this turn.
    - Starts at 0.85
    - Each failed tool: -0.10
    - Empty reply: -0.20
    - Hallucination flag: -0.30
    - Clean execution, no failures: +0.05 bonus
    """
    if not tool_results and not reply:
        return 0.3

    total    = max(len(tool_results), 1)
    failures = sum(1 for r in tool_results if not r.get("ok", True))
    score    = 0.85 - (failures / total) * 0.40

    if not reply or len(reply.strip()) < 10:
        score -= 0.20

    if failures == 0 and tool_results:
        score += 0.05

    return round(max(0.1, min(1.0, score)), 3)


# ── Error log ─────────────────────────────────────────────────────────────────

def _log_errors(tool_results: list, cid: str):
    """Write tool failures to mistakes table for L9/cold processor pickup."""
    failures = [r for r in tool_results if not r.get("ok", True)]
    if not failures:
        return

    for f in failures[:3]:
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
    """
    global _evo_counter

    # Rate limit
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
        from core_github import notify

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
            _evo_counter[cid] = count + 1
            notify(
                f"🧬 <b>Evolution Proposed (L7)</b>\n"
                f"Type: {parsed.get('change_type')}\n"
                f"Summary: {parsed.get('summary','')[:200]}\n"
                f"Confidence: {parsed.get('confidence')}\n"
                f"Reason: {parsed.get('reason','')[:200]}\n"
                f"Review: /review or t_check_evolutions"
            )
            print(f"[L7] Evolution proposed: {parsed.get('summary','')[:80]}")

    except json.JSONDecodeError as e:
        print(f"[L7] Evolution JSON parse failed (non-fatal): {e}")
    except Exception as e:
        print(f"[L7] Evolution proposal error (non-fatal): {e}")


# ── Quality alert ─────────────────────────────────────────────────────────────

def _write_quality_alert(quality: float, cid: str, reason: str):
    try:
        from core_config import sb_post
        sb_post("sessions", {
            "summary":       f"[QUALITY_ALERT] score={quality} reason={reason}",
            "actions":       [f"quality_alert triggered at {datetime.utcnow().isoformat()}"],
            "interface":     "orchestrator_v2",
            "domain":        "core_agi",
            "quality_score": quality,
            "created_at":    datetime.utcnow().isoformat(),
        })
        print(f"[L7] Quality alert written: score={quality}")
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
    """
    cid = intent["sender_id"]

    # Score this turn
    quality = _score_turn(tool_results, reply)
    print(f"[L7] Turn quality: {quality} tools={len(tool_results)} "
          f"failures={sum(1 for r in tool_results if not r.get('ok', True))}")

    # Log tool errors to mistakes table
    _log_errors(tool_results, cid)

    # Quality alert
    if quality < QUALITY_ALERT_THRESHOLD:
        reason = f"{sum(1 for r in tool_results if not r.get('ok',True))} tool failures"
        _write_quality_alert(quality, cid, reason)

    # Check for evolution opportunities (in background thread — non-blocking)
    import threading
    threading.Thread(
        target=_maybe_propose_evolution,
        args=(intent["text"], tool_results, reply, quality, cid),
        daemon=True,
    ).start()

    return quality


if __name__ == "__main__":
    print("🛰️ Layer 7: Observability — Online.")
