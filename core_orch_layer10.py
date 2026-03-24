"""
core_orch_layer10.py — L10: Output Delivery
Final layer. Formats and dispatches to Telegram / MCP / system log.
Uses real core_github.notify — no mocks.
"""
import json
import html
import asyncio
import os
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




def _split_message(text: str, max_len: int = _TG_MAX) -> list:
    """GAP-NEW-27: split long text on newlines into chunks <= max_len."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks if chunks else [text[:max_len]]


def _notify_with_reply(text: str, chat_id: int, reply_to=None) -> bool:
    """GAP-NEW-26: send with reply threading."""
    try:
        import requests
        token = os.getenv("TELEGRAM_TOKEN", "")
        if not token:
            return notify(text, cid=chat_id)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return notify(text, cid=chat_id)


async def _send_followup(text: str, chat_id: int) -> None:
    """GAP-NEW-23: async follow-up chunk."""
    import asyncio, requests
    await asyncio.sleep(0.3)
    try:
        token = os.getenv("TELEGRAM_TOKEN", "")
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as exc:
        print(f"[L10] follow-up send failed: {exc}")


async def _write_history_turn(chat_id: int, role: str, content: str) -> None:
    """GAP-NEW-5: persist turn to conversation_history."""
    try:
        from core_config import sb_post
        sb_post("conversation_history", {"chat_id": chat_id, "role": role, "content": content[:800]})
    except Exception as exc:
        print(f"[L10] history write failed (non-fatal): {exc}")


async def _log_conversation(chat_id: int, user_text: str, response: str, username: str) -> None:
    """GAP-NEW-29: write conversation log row."""
    try:
        from core_config import sb_post
        sb_post("conversation_log", {"chat_id": chat_id, "user": username,
            "user_message": user_text[:500], "core_response": response[:1000]})
    except Exception as exc:
        print(f"[L10] conversation_log write failed (non-fatal): {exc}")


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
            chunks = _split_message(text)
            reply_id = msg.context.get("telegram_message_id")
            if len(chunks) == 1 and len(chunks[0]) > 1500:
                mid = len(chunks[0]) // 2
                cut = chunks[0].rfind("\n", 0, mid) or mid
                p1, p2 = chunks[0][:cut].strip(), chunks[0][cut:].strip()
                ok = _notify_with_reply(p1, msg.chat_id, reply_id)
                if ok and p2:
                    asyncio.ensure_future(_send_followup(p2, msg.chat_id))
            elif len(chunks) > 1:
                ok = _notify_with_reply(chunks[0], msg.chat_id, reply_id)
                for extra in chunks[1:]:
                    asyncio.ensure_future(_send_followup(extra, msg.chat_id))
            else:
                ok = _notify_with_reply(chunks[0], msg.chat_id, reply_id)
            msg.final_output = text
            print(f"[L10] Telegram notify ok={ok}  chunks={len(chunks)}  len={len(text)}")
            asyncio.ensure_future(_log_conversation(msg.chat_id, msg.text, text, msg.user))
            asyncio.ensure_future(_write_history_turn(msg.chat_id, "user", msg.text))
            asyncio.ensure_future(_write_history_turn(msg.chat_id, "assistant", text))

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
