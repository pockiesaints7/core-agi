"""
core_orch_layer7.py — L7: Self-Refinement & Evolution
Analyses interaction for improvement opportunities.
Writes real evolution proposals to Supabase evolution_queue.
No mocks.
"""
import json
from typing import Any, Dict

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_MODEL, sb_post_critical

_REFINE_SYSTEM = (
    "You are CORE AGI's self-improvement engine. "
    "Analyse an interaction and decide if it reveals a reusable improvement. "
    "Return ONLY valid JSON. No preamble."
)

_REFINE_TEMPLATE = """
USER REQUEST: {text}
INTENT: {intent}
TIER: {tier}
TOOL RESULTS (summary): {tool_summary}
ERRORS: {errors}
DOMAIN: {domain}

Did this interaction reveal:
- A pattern worth encoding in the knowledge base?
- A tool that behaved unexpectedly?
- A planning gap?
- A repeated mistake?

Return JSON:
{{
  "propose_evolution": true|false,
  "confidence": 0.0-1.0,
  "change_type": "knowledge|code|behavior|tool",
  "change_summary": "one sentence description of the improvement",
  "pattern_key": "short unique key for dedup",
  "recommendation": "what should change and how",
  "domain": "domain string"
}}
"""


def _summarise_tool_results(msg: OrchestratorMessage) -> str:
    """GAP-NEW-19: preserve error context ? error fields take priority, larger snippet."""
    if not msg.tool_results:
        return "none"
    parts = []
    for r in msg.tool_results[:6]:
        tool = r.get("tool", "?")
        ok = r.get("success", False)
        result = r.get("result", {})
        if isinstance(result, dict):
            # Error fields always surfaced fully
            err = result.get("error") or result.get("message") or ""
            summary = result.get("summary") or ""
            if not ok and err:
                snippet = f"ERROR: {err}"[:300]
            elif summary:
                snippet = str(summary)[:200]
            else:
                keys = [k for k in result.keys() if k not in ("ok","status")]
                snippet = str({k: result[k] for k in keys[:4]})[:200]
        else:
            snippet = str(result)[:200]
        parts.append(f"{tool}(ok={ok}): {snippet}")
    return "\n".join(parts)


async def layer_7_refine(msg: OrchestratorMessage):
    """
    Owner-tier interactions only.
    Skipped if errors are present (don't learn from broken runs).
    Uses Groq to detect evolution opportunities.
    Writes proposals to evolution_queue (confidence >= 0.6).
    """
    msg.track_layer("L7-START")

    # Only analyse owner-tier, error-free, non-trivial interactions
    if msg.tier != "owner":
        msg.track_layer("L7-SKIP-TIER")
        from core_orch_layer8 import layer_8_safety
        await layer_8_safety(msg)
        return

    if msg.has_errors and len(msg.tool_results) == 0:
        msg.track_layer("L7-SKIP-ERRORS")
        from core_orch_layer8 import layer_8_safety
        await layer_8_safety(msg)
        return

    # GAP-NEW-18: skip read-only status queries ? no learning signal
    READ_ONLY = {"system_health","system_state","task_list","evolution_list",
                 "kb_search","mistake_list","deploy_status","greeting","help",
                 "general_query","conversation"}
    if msg.intent in READ_ONLY:
        msg.track_layer("L7-SKIP-READONLY")
        from core_orch_layer8 import layer_8_safety
        await layer_8_safety(msg)
        return

    if not msg.tool_results and not msg.has_errors:
        msg.track_layer("L7-SKIP-TRIVIAL")
        from core_orch_layer8 import layer_8_safety
        await layer_8_safety(msg)
        return

    try:
        errors_summary = (
            " | ".join(e["message"][:80] for e in msg.errors[:3])
            if msg.errors else "none"
        )
        prompt = _REFINE_TEMPLATE.format(
            text=msg.text[:400],
            intent=msg.intent or "unknown",
            tier=msg.tier,
            tool_summary=_summarise_tool_results(msg),
            errors=errors_summary,
            domain=msg.context.get("current_domain", "general"),
        )
        raw = groq_chat(
            system=_REFINE_SYSTEM,
            user=prompt,
            model=GROQ_MODEL,
            max_tokens=300,
        )
        analysis = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())

        if analysis.get("propose_evolution") and analysis.get("confidence", 0) >= 0.6:
            # Dedup gate: skip if a pending entry with this pattern_key already exists
            from core_config import sb_get
            pattern_key = analysis.get("pattern_key", msg.text[:100])
            existing = sb_get(
                "evolution_queue",
                f"select=id&pattern_key=eq.{pattern_key}&status=eq.pending&limit=1",
            )
            if existing:
                print(f"[L7] Skipped duplicate evo (pending): {pattern_key[:60]}")
                msg.track_layer("L7-DEDUP-SKIP")
                from core_orch_layer8 import layer_8_safety
                await layer_8_safety(msg)
                return

            # Write to evolution_queue
            ok = sb_post_critical("evolution_queue", {
                "change_type":    analysis.get("change_type", "knowledge"),
                "change_summary": analysis.get("change_summary", "")[:300],
                "pattern_key":    pattern_key,
                "confidence":     float(analysis.get("confidence", 0.6)),
                "status":         "pending",
                "source":         "orchestrator_l7",
                "impact":         analysis.get("domain", msg.context.get("current_domain", "general")),
                "recommendation": analysis.get("recommendation", "")[:500],
            })
            if ok:
                msg.evolutions_proposed.append(analysis)
                print(f"[L7] Evolution queued: {analysis.get('change_summary','')[:80]}")
            else:
                print(f"[L7] Evolution write failed (non-fatal)")
        else:
            print(f"[L7] No evolution proposed (conf={analysis.get('confidence',0):.2f})")

    except Exception as exc:
        print(f"[L7] Refinement check failed (non-fatal): {exc}")

    msg.track_layer("L7-COMPLETE")

    from core_orch_layer8 import layer_8_safety
    await layer_8_safety(msg)
