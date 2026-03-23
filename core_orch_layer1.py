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
import threading
import traceback

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
_CONFIRM_YES = {"confirm", "yes", "y", "ok", "go", "do it", "proceed", "lanjut"}
_CONFIRM_NO  = {"reject", "cancel", "no", "n", "stop", "abort", "skip", "batal"}

# Command prefixes — only these are routed to the legacy handler.
# Anything else gets an immediate "unknown command" reply — never enters the pipeline.
_COMMANDS = {"/start", "/status", "/tstatus", "/project", "/help",
             "/clear", "/listen", "/backfill", "/mine"}

# ── In-flight dedup ───────────────────────────────────────────────────────────
# Tracks (message_id, chat_id) pairs currently being processed.
# Prevents Telegram re-delivery from spawning duplicate pipeline runs.
_IN_FLIGHT: set      = set()
_IN_FLIGHT_LOCK      = threading.Lock()

# Drop Telegram messages older than this many seconds.
# Guards against backlogged re-deliveries after a crash/restart.
_MAX_MSG_AGE_S = 60   # generous: 60s covers Railway cold-start redeliveries


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """
    Extract all known attachment types from a Telegram message.
    Unrecognised media types (sticker, video_note, animation, video, audio)
    are captured as 'unsupported' so routing can handle them gracefully
    instead of falling through to an empty-text trivial call.
    """
    atts = []

    if "photo" in msg:
        photos = msg["photo"]
        best = max(photos, key=lambda p: p.get("file_size", 0))
        atts.append({"type": "image", "file_id": best["file_id"],
                     "mime_type": "image/jpeg"})

    if "document" in msg:
        doc = msg["document"]
        atts.append({"type": "file", "file_id": doc["file_id"],
                     "mime_type": doc.get("mime_type", "application/octet-stream"),
                     "file_name": doc.get("file_name", "")})

    if "voice" in msg:
        atts.append({"type": "voice", "file_id": msg["voice"]["file_id"],
                     "mime_type": "audio/ogg"})

    # Capture unsupported media so we can reply gracefully instead of
    # routing them as empty-text trivial/conversation messages.
    for media_key in ("sticker", "video_note", "animation", "video", "audio"):
        if media_key in msg:
            media = msg[media_key]
            file_id = (media.get("file_id") if isinstance(media, dict)
                       else media[0].get("file_id") if isinstance(media, list) else "")
            atts.append({"type": "unsupported", "subtype": media_key,
                         "file_id": file_id, "mime_type": ""})

    return atts


def build_intent(msg: dict) -> dict:
    """Build typed Intent object from raw Telegram message."""
    sender_id   = str(msg.get("chat", {}).get("id", ""))
    sender_name = (msg.get("from", {}).get("username", "")
                   or msg.get("from", {}).get("first_name", "unknown"))
    raw_text    = (msg.get("text") or msg.get("caption") or "").strip()
    attachments = _extract_attachments(msg)
    ts          = msg.get("date", time.time())

    # Strip @botname suffix from commands (e.g. /status@MyBot → /status)
    if raw_text.startswith("/") and "@" in raw_text:
        raw_text = raw_text.split("@")[0]

    # Determine message type
    if attachments:
        first_type = attachments[0]["type"]
        msg_type = first_type if first_type != "unsupported" else "message"
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


# ── Entry point ───────────────────────────────────────────────────────────────

async def layer_1_triage(msg: dict):
    """
    Entry point. Builds Intent, applies guards, routes to correct handler.
    Called from core_main.py handle_msg().

    Guard order (cheapest first):
      1. Stale message age      — drop before any processing
      2. In-flight dedup        — drop exact re-deliveries
      3. Rate limit (L0)        — throttle bursts
      4. Tier / auth gate       — block anon non-trivial
      5. Route dispatch
    """
    from core_orch_layer0 import LIMITER

    # ── Guard 1: stale message age ────────────────────────────────────────────
    # Telegram re-delivers updates if the offset is not advanced (polling mode)
    # or if the webhook times out. Drop anything too old.
    msg_ts = msg.get("date")   # Unix timestamp set by Telegram servers
    if msg_ts and (time.time() - msg_ts) > _MAX_MSG_AGE_S:
        age = int(time.time() - msg_ts)
        print(f"[L1] Dropping stale message (age={age}s > {_MAX_MSG_AGE_S}s)")
        return

    # ── Guard 2: in-flight dedup ──────────────────────────────────────────────
    # Build a stable key from (message_id, chat_id). Both needed: message_id
    # is only unique within a chat; chat_id scopes it globally.
    raw_msg_id = msg.get("message_id")
    raw_chat_id = msg.get("chat", {}).get("id", "")
    if raw_msg_id is None:
        # Service messages / channel posts may lack message_id — use ts+chat as key
        raw_msg_id = f"svc_{msg_ts}_{msg.get('update_id', '')}"
    msg_key = f"{raw_msg_id}_{raw_chat_id}"

    with _IN_FLIGHT_LOCK:
        if msg_key in _IN_FLIGHT:
            print(f"[L1] Duplicate in-flight key={msg_key} — dropping")
            return
        _IN_FLIGHT.add(msg_key)

    try:
        # ── Guard 3: rate limit ───────────────────────────────────────────────
        if not LIMITER.consume():
            cid_for_notify = str(raw_chat_id)
            print(f"[L1] Rate limit hit — dropping (chat={cid_for_notify})")
            # Notify owner so silence is not mysterious
            if cid_for_notify == str(OWNER_ID):
                try:
                    from core_config import notify
                    notify("⚠️ Rate limit active — message dropped. Try again shortly.",
                           cid_for_notify)
                except Exception:
                    pass
            return

        intent = build_intent(msg)
        cid    = intent["sender_id"]

        print(f"[L1] intent_id={intent['intent_id'][:8]} route={intent['route']} "
              f"tier={intent['tier']} text={intent['text'][:60]!r}")

        # ── Guard 4: auth gate ────────────────────────────────────────────────
        if intent["tier"] == "anon" and intent["route"] != "trivial":
            print(f"[L1] Blocked: anon sender {cid} tried non-trivial route")
            from core_config import notify
            notify("⛔ Unauthorized.", cid)
            return

        # ── Guard 5: unsupported media — reply and stop ───────────────────────
        if (intent["attachments"]
                and intent["attachments"][0]["type"] == "unsupported"
                and not intent["text"]):
            subtype = intent["attachments"][0].get("subtype", "media")
            from core_config import notify
            notify(f"⚠️ Unsupported media type: `{subtype}`. "
                   f"Send text, photo, document, or voice.", cid)
            return

        # ── Dispatch ──────────────────────────────────────────────────────────
        try:
            if intent["route"] == "command":
                await _handle_command(intent)
            elif intent["route"] == "confirm":
                await _handle_confirm(intent)
            elif intent["route"] == "trivial":
                await _handle_trivial(intent)
            else:
                from core_orch_layer2 import layer_2_hydrate
                await layer_2_hydrate(intent)

        except Exception as e:
            print(f"[L1] Unhandled dispatch error: {e}\n{traceback.format_exc()}")
            try:
                from core_config import notify
                notify(f"⚠️ Internal error: {e}", cid)
            except Exception:
                pass

    finally:
        # Always release dedup key — even if an exception propagated
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(msg_key)


# ── Route handlers ─────────────────────────────────────────────────────────────

async def _handle_command(intent: dict):
    """
    Dispatch known slash commands to the legacy handler.
    Immediately reply for unknown commands — NEVER enter the pipeline.
    This is the primary fix for the spam loop: unknown commands were
    previously routed through L2→L3→L4 which failed and re-queued.
    """
    # Extract base command (first token, already @-stripped in build_intent)
    cmd = intent["text"].split()[0].lower()

    if cmd not in _COMMANDS:
        from core_config import notify
        known_list = "  ".join(sorted(_COMMANDS))
        notify(
            f"❓ Unknown command: `{cmd}`\n\nAvailable commands:\n{known_list}",
            intent["sender_id"],
        )
        print(f"[L1] Unknown command '{cmd}' — replied inline, pipeline skipped")
        return

    # Known command → delegate to existing core_main handler in a thread.
    # core_main.handle_msg is synchronous; run it in a daemon thread to
    # avoid blocking the async event loop.
    from core_main import handle_msg as _legacy_handle
    t = threading.Thread(target=_legacy_handle,
                         args=(intent["raw_msg"],), daemon=True)
    t.start()


async def _handle_confirm(intent: dict):
    """
    Route CONFIRM/REJECT to the pending confirmation gate.
    Guard against orphaned confirms (no pending gate) to avoid silent failures.
    """
    from core_orch_layer5 import receive_confirmation
    try:
        await receive_confirmation(intent["sender_id"], intent["text"])
    except Exception as e:
        # receive_confirmation may raise if no gate is pending
        print(f"[L1] Confirmation handler error: {e}")
        try:
            from core_config import notify
            notify("⚠️ No pending confirmation found, or confirmation error.", 
                   intent["sender_id"])
        except Exception:
            pass


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
        "tools":   [],
    }
    try:
        reply = await call_model_simple(intent["text"], minimal_context)
    except Exception as e:
        print(f"[L1] Trivial model call failed: {e}\n{traceback.format_exc()}")
        reply = f"⚠️ Model error: {e}"

    await layer_5_output(intent, reply, tool_results=[])


if __name__ == "__main__":
    print("🛰️ Layer 1: Input / Triage — Online.")
