# GAP-NEW-10: cached tool list
_TOOL_LIST_CACHE: dict = {"list": None, "count": 0}


def _get_cached_tool_list() -> tuple:
    if _TOOL_LIST_CACHE["list"] is not None:
        return _TOOL_LIST_CACHE["list"], _TOOL_LIST_CACHE["count"]
    try:
        from core_tools import TOOLS
        from core_config import TOOL_CATEGORY_KEYWORDS
        cats: dict = {cat: [] for cat in TOOL_CATEGORY_KEYWORDS}
        cats["misc"] = []
        for tn, fn in TOOLS.items():
            doc = (fn.__doc__ or "").split("\n")[0][:60]
            placed = False
            for cat, kws in TOOL_CATEGORY_KEYWORDS.items():
                if any(kw in tn for kw in kws):
                    cats[cat].append(f"{tn}: {doc}" if doc else tn)
                    placed = True
                    break
            if not placed:
                cats["misc"].append(tn)
        lines = [f"- {c}: {', '.join(sorted(ts))}" for c, ts in cats.items() if ts]
        result = "\n".join(lines)
        _TOOL_LIST_CACHE["list"] = result
        _TOOL_LIST_CACHE["count"] = len(TOOLS)
        return result, len(TOOLS)
    except Exception:
        return "", 0


"""
core_orch_layer9.py — L9: Tone & Personality
Transforms raw tool results into CORE's natural voice using real Groq.
No mocks.
"""
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_FAST, GROQ_MODEL

_PERSONA_SYSTEM = (
    "You are CORE — an autonomous AGI orchestration system deployed on an Ubuntu VM. "
    "Your tone is: direct, precise, technically confident. No fluff, no filler words, "
    "no unnecessary hedging. Use plain text. Telegram HTML formatting only when it adds clarity "
    "(<b>bold</b>, <code>code</code>). Max 3800 chars."
)

_PERSONA_TEMPLATE = """
USER MESSAGE: {text}
INTENT: {intent}
TOOL RESULTS:
{tool_summary}
ERRORS:
{errors}
DOMAIN: {domain}
TIER: {tier}

Write CORE's response to the user. Be direct. Lead with the answer.
If tools ran, summarise what they returned — don't just echo raw JSON.
If there were errors, say what failed and what to do next.
"""

# For direct-response (no tools), inject live session state so CORE can answer
# questions like "what are you working on?" with real data.
_CONVO_SYSTEM = (
    "You are CORE — an autonomous AGI system. Be brief, direct, technically accurate. "
    "No filler. Plain text unless HTML tags add clarity. Max 1500 chars."
)

_CONVO_TEMPLATE = """
USER: {text}
INTENT: {intent}

SESSION STATE:
{session_state}

Reply as CORE. Keep it short and precise. Use the session state above to answer
questions about current tasks, recent activity, or system status.
"""


def _format_tool_summary(tool_results: List[Dict[str, Any]]) -> str:
    if not tool_results:
        return "No tools executed."
    lines = []
    for r in tool_results:
        tool = r.get("tool", "?")
        ok = r.get("success", False)
        result = r.get("result", {})
        if isinstance(result, dict):
            # Remove bulk fields that pollute the prompt
            trimmed = {
                k: v for k, v in result.items()
                if k not in ("wiring", "chunks", "source", "session_md")
                and not (isinstance(v, list) and len(v) > 20)
            }
            snippet = json.dumps(trimmed, default=str)[:800]
        else:
            snippet = str(result)[:400]
        lines.append(f"[{tool}  ok={ok}]\n{snippet}")
    return "\n\n".join(lines)


def _format_errors(errors: List[Dict[str, Any]]) -> str:
    if not errors:
        return "none"
    return " | ".join(
        f"{e.get('layer','?')}/{e.get('error_code','?')}: {e.get('message','')[:100]}"
        for e in errors[:4]
    )


def _build_session_state(msg: OrchestratorMessage) -> str:
    """
    Build a compact session state summary for conversational Groq calls.
    Pulls from msg.context['session'] (loaded by L2 from session_start).
    Falls back gracefully if session data is sparse.
    """
    ctx = msg.context
    session = ctx.get("session", {})
    lines = []

    # In-progress tasks
    in_progress = session.get("in_progress_tasks", [])
    if in_progress:
        task_strs = []
        for t in in_progress[:5]:
            if isinstance(t, dict):
                name = t.get("task") or t.get("title") or t.get("description") or "?"
                task_strs.append(f"  - [{t.get('id','?')}] {str(name)[:80]} (priority={t.get('priority','?')})")
            else:
                task_strs.append(f"  - {str(t)[:80]}")
        lines.append("In-progress tasks:\n" + "\n".join(task_strs))
    else:
        lines.append("In-progress tasks: none")

    # Last session summary
    last = session.get("last_session", {})
    if last:
        summary = last.get("summary", last.get("last_session_summary", ""))
        if summary:
            lines.append(f"Last session: {str(summary)[:300]}")

    # Health snapshot
    health = session.get("health", {})
    if health:
        statuses = []
        for svc, st in health.items():
            if isinstance(st, dict):
                statuses.append(f"{svc}={st.get('status','?')}")
            else:
                statuses.append(f"{svc}={st}")
        if statuses:
            lines.append("Health: " + ", ".join(statuses[:6]))

    # Quality alert
    quality = session.get("quality_alert")
    if quality:
        lines.append(f"Quality alert: {json.dumps(quality, default=str)[:150]}")

    # Pending evolutions count
    evos = ctx.get("pending_evolutions", [])
    if evos:
        lines.append(f"Pending evolutions: {len(evos)}")

    # Domain mistakes count
    mistakes = ctx.get("domain_mistakes", [])
    if mistakes:
        lines.append(f"Domain mistakes loaded: {len(mistakes)}")

    if not lines or lines == ["In-progress tasks: none"]:
        return "Session state not loaded (no session_start data in context)."

    return "\n".join(lines)


async def layer_9_tone(msg: OrchestratorMessage):
    """
    Generate the styled CORE response.
    Uses Groq for both tool-result summarisation and pure conversation.
    Falls back to plain text summary on Groq failure.
    """
    msg.track_layer("L9-START")
    print(f"[L9] Styling response ...")

    try:
        tool_summary = _format_tool_summary(msg.tool_results)
        errors_str = _format_errors(msg.errors)

        if msg.tool_results or msg.has_errors:
            # Tool-driven response
            prompt = _PERSONA_TEMPLATE.format(
                text=msg.text[:400],
                intent=msg.intent or "unknown",
                tool_summary=tool_summary,
                errors=errors_str,
                domain=msg.context.get("current_domain", "general"),
                tier=msg.tier,
            )
            styled = groq_chat(
                system=_PERSONA_SYSTEM,
                user=prompt,
                model=GROQ_MODEL,
                max_tokens=1200,
            )
        else:
            # Pure conversational / direct-response
            direct_answer = msg.plan.get("direct_answer") if msg.plan else None
            if direct_answer:
                # L4 already supplied the answer — just let CORE voice it
                styled = direct_answer
            else:
                # Inject live session state so CORE can answer "what are you working on?" etc.
                session_state = _build_session_state(msg)
                prompt = _CONVO_TEMPLATE.format(
                    text=msg.text[:400],
                    intent=msg.intent or "conversation",
                    session_state=session_state,
                )
                styled = groq_chat(
                    system=_CONVO_SYSTEM,
                    user=prompt,
                    model=GROQ_FAST,
                    max_tokens=600,
                )

        # GAP-NEW-12: append preflight warning note if present
        pf_note = msg.context.get("preflight_warning_note", "")
        msg.styled_response = (styled.strip() + "\n\n" + pf_note).strip() if pf_note else styled.strip()

    except Exception as exc:
        print(f"[L9] Groq styling failed — using plain fallback: {exc}")
        # Plain-text fallback: dump tool results without LLM
        if msg.tool_results:
            lines = [f"OK {r['tool']}: " + (
                str(r.get("result", ""))[:300] if not isinstance(r.get("result"), dict)
                else json.dumps(r["result"], default=str)[:300]
            ) for r in msg.tool_results]
            msg.styled_response = "\n".join(lines)
        elif msg.has_errors:
            msg.styled_response = "ERR " + " | ".join(
                e["message"][:120] for e in msg.errors[:3]
            )
        else:
            msg.styled_response = "Done."

    msg.track_layer("L9-COMPLETE")
    print(f"[L9] Response ready ({len(msg.styled_response or '')} chars)")

    from core_orch_layer10 import layer_10_output
    await layer_10_output(msg)
