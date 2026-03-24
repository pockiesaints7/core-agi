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

# For direct-response (no tools), just answer conversationally with Groq
_CONVO_SYSTEM = (
    "You are CORE — an autonomous AGI system. Be brief, direct, technically accurate. "
    "No filler. Plain text unless HTML tags add clarity. Max 1500 chars."
)

_CONVO_TEMPLATE = """
USER: {text}
INTENT: {intent}
CONTEXT SUMMARY: {context_hint}

Reply as CORE. Keep it short and precise.
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


async def layer_9_tone(msg: OrchestratorMessage):
    """
    Generate the styled CORE response.
    Uses Groq for both tool-result summarisation and pure conversation.
    Falls back to plain text summary on Groq failure.
    """
    msg.track_layer("L9-START")
    print(f"[L9] Styling response …")

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
            direct_answer = msg.plan.get("direct_answer")
            if direct_answer:
                # L4 already supplied the answer — just let CORE voice it
                styled = direct_answer
            else:
                context_hint = (
                    f"last_session={msg.context.get('session', {}).get('last_session_summary', '')[:200]}"
                )
                prompt = _CONVO_TEMPLATE.format(
                    text=msg.text[:400],
                    intent=msg.intent or "conversation",
                    context_hint=context_hint,
                )
                styled = groq_chat(
                    system=_CONVO_SYSTEM,
                    user=prompt,
                    model=GROQ_FAST,
                    max_tokens=600,
                )

        msg.styled_response = styled.strip()

    except Exception as exc:
        print(f"[L9] Groq styling failed — using plain fallback: {exc}")
        # Plain-text fallback: dump tool results without LLM
        if msg.tool_results:
            lines = [f"✅ {r['tool']}: " + (
                str(r.get("result", ""))[:300] if not isinstance(r.get("result"), dict)
                else json.dumps(r["result"], default=str)[:300]
            ) for r in msg.tool_results]
            msg.styled_response = "\n".join(lines)
        elif msg.has_errors:
            msg.styled_response = "❌ " + " | ".join(
                e["message"][:120] for e in msg.errors[:3]
            )
        else:
            msg.styled_response = "Done."

    msg.track_layer("L9-COMPLETE")
    print(f"[L9] Response ready ({len(msg.styled_response or '')} chars)")

    from core_orch_layer10 import layer_10_output
    await layer_10_output(msg)
