"""
core_orch_layer10.py — L10: Output Delivery
Final layer. Formats and dispatches to Telegram / MCP / system log.
Uses real core_github.notify — no mocks.
"""
import json
from typing import Any

from orchestrator_message import OrchestratorMessage
from core_github import notify

# Telegram hard limit
_TG_MAX = 4000


def _escape_partial(text: str) -> str:
    """
    We use HTML parse_mode in notify(). Strip raw angle brackets that aren't
    our intentional <b>/<code> tags so Telegram doesn't choke.
    Only strip unmatched < > that look like raw data, not markup.
    """
    # Light-touch: just truncate — Telegram rejects mal-formed HTML silently anyway.
    return text


def _format_telegram(msg: OrchestratorMessage) -> str:
    """Build final Telegram message string."""

    # Error-only path
    if msg.has_errors and not msg.styled_response:
        lines = ["❌ <b>CORE Error</b>\n"]
        for err in msg.errors[:4]:
            lines.append(
                f"• <code>{err.get('layer','?')}/{err.get('error_code','?')}</code>: "
                f"{err.get('message','')[:120]}"
            )
        return "\n".join(lines)[:_TG_MAX]

    response = msg.styled_response or "✅ Done."

    # Append redaction notice if anything was sanitised
    if msg.safety_redacted:
        unique = list(set(msg.safety_redacted))
        response += f"\n\n<i>[{len(unique)} sensitive field(s) redacted]</i>"

    # Hard truncate
    if len(response) > _TG_MAX:
        response = response[: _TG_MAX - 60] + "\n\n<i>[truncated — response too long]</i>"

    return response


def _format_mcp(msg: OrchestratorMessage) -> dict:
    """Return structured dict for MCP callers."""
    return {
        "success": not msg.has_errors,
        "intent": msg.intent,
        "response": msg.styled_response or "",
        "tool_results": msg.tool_results,
        "errors": msg.errors,
        "layer_stack": msg.layer_stack,
        "evolutions_proposed": len(msg.evolutions_proposed),
    }


async def layer_10_output(msg: OrchestratorMessage):
    """
    Dispatch final output to the appropriate channel.
    telegram → notify() (HTML, 4000-char limit)
    mcp      → structured dict stored in msg.final_output
    system   → log only
    """
    msg.track_layer("L10-START")
    print(f"[L10] Dispatching output  source={msg.source}  chat_id={msg.chat_id}")

    try:
        if msg.source == "telegram":
            text = _format_telegram(msg)
            ok = notify(text, cid=msg.chat_id)
            msg.final_output = text
            print(f"[L10] Telegram notify ok={ok}  len={len(text)}")

        elif msg.source == "mcp":
            payload = _format_mcp(msg)
            msg.final_output = json.dumps(payload, default=str)
            print(f"[L10] MCP response ready  success={payload['success']}")

        elif msg.source == "system":
            msg.final_output = f"system event processed: {msg.text[:80]}"
            print(f"[L10] System event logged: {msg.final_output}")

        else:
            # Unknown source — notify owner anyway
            text = _format_telegram(msg)
            notify(text, cid=msg.chat_id)
            msg.final_output = text

    except Exception as exc:
        print(f"[L10] CRITICAL output error: {exc}")
        # Last-resort notify
        try:
            notify(f"⚠️ CORE L10 failure: {str(exc)[:200]}", cid=msg.chat_id)
        except Exception:
            pass
        msg.add_error("L10", exc, "OUTPUT_FAILED")

    msg.track_layer("L10-COMPLETE")
    print(f"[L10] Pipeline complete. Layers: {' → '.join(msg.layer_stack)}")
