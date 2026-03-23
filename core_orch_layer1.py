"""
core_orch_layer1.py — L1: INPUT / TRIAGE
=========================================
Receives the raw Telegram message, builds a typed Intent object,
classifies it (trivial vs. non-trivial), and routes it.

Routes:
  command      → /start /status /tstatus etc. → handled inline
  trivial      → short factual query → skip brain hydration, go L2 fast-path
  conversation → normal message → full L2 context hydration
  confirm      → CONFIRM/REJECT reply to a pending gate → confirmation handler
  file/image   → attachment → extract + route to conversation

Intent object passed downstream:
  {
    "intent_id":   str,          # uuid
    "source":      "telegram",
    "sender_id":   str,          # chat_id
    "sender_name": str,          # username
    "tier":        "owner|trusted|anon",
    "type":        "command|message|file|image|voice|confirm",
    "text":        str,          # cleaned text
    "raw_msg":     dict,         # original telegram message object
    "attachments": list,         # [{type, file_id, mime_type}]
    "route":       "command|trivial|conversation|confirm",
    "is_trivial":  bool,
    "ts":          float
  }
"""

import os
import re
import time
import uuid
import asyncio
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────
OWNER_ID = os.environ.get("TELEGRAM_CHAT", "")

# Patterns that mean: skip expensive brain hydration, answer directly
_TRIVIAL_PATTERNS = [
    r'^\s*(hi|hello|hey|halo|hai|selamat|good\s)',
    r'\bping\b',
    r'\btime\b|\bjam\b|\bwaktu\b',
    r'\bweather\b|\bcuaca\b',
    r'^\s*[\d\s\+\-\*\/\(\)\.%]+\s*$',   # pure math
    r'\btranslate\b|\bterjemah',
    r'\bprice\b|\bharga\b',
    r'\bcurrency\b|\bkurs\b',
]
_TRIVIAL_RE = [re.compile(p, re.IGNORECASE) for p in _TRIVIAL_PATTERNS]
_TRIVIAL_MAX_LEN = 35

# Keywords that force non-trivial even on short messages
_NON_TRIVIAL_KW = [
    "task", "deploy", "patch", "error", "fix", "broken", "kb", "knowledge",
    "mistake", "pattern", "session", "analyze", "train", "cold", "hot",
    "build", "railway", "supabase", "github", "tool", "code", "why", "how",
    "tugas", "kenapa", "gimana", "tolong", "coba", "salah",
]

# Confirmation keywords (responding to a destructive-op gate)
_CONFIRM_YES  = {"confirm", "yes", "y", "ok", "go", "do it", "proceed", "lanjut"}
_CONFIRM_NO   = {"reject", "cancel", "no", "n", "stop", "abort", "skip", "batal"}

# Command prefixes
_COMMANDS = {"/start", "/status", "/tstatus", "/project", "/help",
             "/clear", "/listen", "/backfill", "/mine"}


def _is_trivial(text: str) -> bool:
    t = text.strip()
    lower = t.lower()
    if any(kw in lower for kw in _NON_TRIVIAL_KW):
        return False
    for p in _TRIVIAL_RE:
        if p.search(t):
            return True
    return len(t) <= _TRIVIAL_MAX_LEN


def _get_tier(sender_id: str) -> str:
    """Owner vs anon. Extend with trusted list in Supabase later."""
    if str(sender_id) == str(OWNER_ID):
        return "owner"
    return "anon"


def _extract_attachments(msg: dict) -> list:
    atts = []
    if "photo" in msg:
        photos = msg["photo"]
        best = max(photos, key=lambda p: p.get("file_size", 0))
        atts.append({"type": "image", "file_id": best["file_id"], "mime_type": "image/jpeg"})
    if "document" in msg:
        doc = msg["document"]
        atts.append({"type": "file", "file_id": doc["file_id"],
                     "mime_type": doc.get("mime_type", "application/octet-stream"),
                     "file_name": doc.get("file_name", "")})
    if "voice" in msg:
        atts.append({"type": "voice", "file_id": msg["voice"]["file_id"],
                     "mime_type": "audio/ogg"})
    return atts


def build_intent(msg: dict) -> dict:
    """Build typed Intent object from raw Telegram message."""
    sender_id   = str(msg.get("chat", {}).get("id", ""))
    sender_name = msg.get("from", {}).get("username", "") or msg.get("from", {}).get("first_name", "unknown")
    raw_text    = (msg.get("text") or msg.get("caption") or "").strip()
    attachments = _extract_attachments(msg)
    ts          = msg.get("date", time.time())

    # Strip @botname suffix from commands
    if raw_text.startswith("/") and "@" in raw_text:
        raw_text = raw_text.split("@")[0]

    # Determine message type
    if attachments:
        msg_type = attachments[0]["type"]    # image / file / voice
    elif raw_text.startswith("/"):
        msg_type = "command"
    else:
        msg_type = "message"

    # Determine route
    lower = raw_text.strip().lower()
    if msg_type == "command":
        route = "command"
    elif lower in _CONFIRM_YES or lower in _CONFIRM_NO:
        route = "confirm"
    elif _is_trivial(raw_text) and not attachments:
        route = "trivial"
    else:
        route = "conversation"

    return {
        "intent_id":   str(uuid.uuid4()),
        "source":      "telegram",
        "sender_id":   sender_id,
        "sender_name": sender_name,
        "tier":        _get_tier(sender_id),
        "type":        msg_type,
        "text":        raw_text,
        "raw_msg":     msg,
        "attachments": attachments,
        "route":       route,
        "is_trivial":  route == "trivial",
        "ts":          float(ts),
    }


async def layer_1_triage(msg: dict):
    """
    Entry point. Builds Intent, applies L0 rate-limit, routes to correct handler.
    Called from core_main.py handle_msg() in a thread.
    """
    from core_orch_layer0 import LIMITER, OWNER_ID as L0_OWNER

    # L0 gate: rate limit
    if not LIMITER.consume():
        print(f"[L1] Rate limit hit — dropping message")
        return

    intent = build_intent(msg)
    cid    = intent["sender_id"]

    print(f"[L1] intent_id={intent['intent_id'][:8]} route={intent['route']} "
          f"tier={intent['tier']} text={intent['text'][:60]!r}")

    # L0 gate: owner-only for non-anon actions
    if intent["tier"] == "anon" and intent["route"] != "trivial":
        print(f"[L1] Blocked: anon sender {cid} tried non-trivial route")
        from core_config import notify
        notify("⛔ Unauthorized.", cid)
        return

    try:
        if intent["route"] == "command":
            await _handle_command(intent)
        elif intent["route"] == "confirm":
            await _handle_confirm(intent)
        elif intent["route"] == "trivial":
            await _handle_trivial(intent)
        else:
            # Full pipeline: L2 → L3 → L4 → L5 → ... → L9
            from core_orch_layer2 import layer_2_hydrate
            await layer_2_hydrate(intent)

    except Exception as e:
        print(f"[L1] Unhandled error: {e}")
        from core_config import notify
        notify(f"⚠️ Internal error at L1: {e}", cid)


# ── Route handlers ─────────────────────────────────────────────────────────────

async def _handle_command(intent: dict):
    """Delegate slash commands to core_main command handler (existing logic)."""
    from core_main import handle_msg as _legacy_handle
    import threading
    # Run existing command handling synchronously in thread
    threading.Thread(target=_legacy_handle, args=(intent["raw_msg"],), daemon=True).start()


async def _handle_confirm(intent: dict):
    """Route CONFIRM/REJECT to the pending confirmation gate."""
    from core_orch_layer5 import receive_confirmation
    await receive_confirmation(intent["sender_id"], intent["text"])


async def _handle_trivial(intent: dict):
    """
    Fast path: skip brain hydration.
    Inject minimal context → L8 model call → L5 output.
    """
    from core_orch_layer8 import call_model_simple
    from core_orch_layer5 import layer_5_output

    print(f"[L1] Trivial fast-path for: {intent['text'][:50]!r}")

    minimal_context = {
        "system": (
            "You are CORE — a sovereign intelligence, not a generic assistant. "
            "Owner: REINVAGNAR (Jakarta, WIB/UTC+7). Answer directly and concisely."
        ),
        "history": [],
        "tools": [],
    }
    try:
        reply = await call_model_simple(intent["text"], minimal_context)
    except Exception as e:
        reply = f"Error: {e}"

    await layer_5_output(intent, reply, tool_results=[])


if __name__ == "__main__":
    print("🛰️ Layer 1: Input / Triage — Online.")
