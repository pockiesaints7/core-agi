"""
core_orch_layer1.py — L1: Input Reception & Triage
Parses raw Telegram/MCP/system payloads into OrchestratorMessage.
Entry point for all traffic.
"""
import os
import asyncio
from typing import Dict, Any
from dotenv import load_dotenv
load_dotenv()
from orchestrator_message import OrchestratorMessage

# ?? Dedup gate ???????????????????????????????????????????????????????????????
# Prevents Telegram webhook retries from processing the same message twice.
# In-memory; resets on service restart (acceptable ? retries are ~30s window).
_PROCESSED_IDS: set[int] = set()
_MAX_DEDUP_SIZE = 2000  # cap memory usage


def _is_duplicate(message_id: int) -> bool:
    if message_id in _PROCESSED_IDS:
        return True
    _PROCESSED_IDS.add(message_id)
    if len(_PROCESSED_IDS) > _MAX_DEDUP_SIZE:
        # Evict oldest half ? sets are unordered so just clear oldest ~half
        to_remove = list(_PROCESSED_IDS)[:_MAX_DEDUP_SIZE // 2]
        for mid in to_remove:
            _PROCESSED_IDS.discard(mid)
    return False


# ?? Typing indicator ??????????????????????????????????????????????????????????
async def _send_typing(chat_id: int) -> None:
    """Fire-and-forget sendChatAction typing ? masks processing latency."""
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        pass  # typing is best-effort ? never block on it


# Known slash-commands that require tool execution
_COMMAND_ROUTES = {
    "/health", "/state", "/status", "/tasks", "/evolutions",
    "/kb", "/mistakes", "/train", "/cold", "/deploy",
    "/listen", "/checkpoint", "/help",
    "/ask", "/search", "/time", "/calc", "/weather", "/tools", "/run", "/do", "/log",
}

# ── L1 NLU Synonym Expansion ─────────────────────────────────────────────────
# Maps natural-language phrases → canonical slash-commands.
# Applied BEFORE route detection. Zero-latency, deterministic.
# Keys are lowercase substrings. First match wins. Longer/more specific first.
_NLU_SYNONYMS: list[tuple[str, str]] = [
    # health / status
    ("check health",       "/health"),
    ("system health",      "/health"),
    ("are you ok",         "/health"),
    ("ping",               "/health"),
    ("what's running",     "/status"),
    ("what is running",    "/status"),
    ("current status",     "/status"),
    ("show status",        "/status"),
    ("system status",      "/status"),
    ("check status",       "/status"),
    ("full state",         "/state"),
    ("show state",         "/state"),
    ("dump state",         "/state"),
    # tasks
    ("show tasks",         "/tasks"),
    ("list tasks",         "/tasks"),
    ("what tasks",         "/tasks"),
    ("open tasks",         "/tasks"),
    ("pending tasks",      "/tasks"),
    ("task list",          "/tasks"),
    # evolutions
    ("show evolutions",    "/evolutions"),
    ("list evolutions",    "/evolutions"),
    ("pending evolutions", "/evolutions"),
    ("what evolutions",    "/evolutions"),
    # knowledge base
    ("search kb",          "/kb"),
    ("show kb",            "/kb"),
    ("knowledge base",     "/kb"),
    # mistakes
    ("show mistakes",      "/mistakes"),
    ("list mistakes",      "/mistakes"),
    ("recent mistakes",    "/mistakes"),
    # training
    ("run training",       "/train"),
    ("start training",     "/train"),
    ("trigger training",   "/train"),
    ("run cold",           "/cold"),
    ("cold processor",     "/cold"),
    ("trigger cold",       "/cold"),
    # deploy
    ("show deploy",        "/deploy"),
    ("deploy status",      "/deploy"),
    ("deployment status",  "/deploy"),
    # listen / checkpoint
    ("start listen",       "/listen"),
    ("listen mode",        "/listen"),
    ("save checkpoint",    "/checkpoint"),
    ("show checkpoint",    "/checkpoint"),
    # help
    ("show help",          "/help"),
    ("list commands",      "/help"),
    ("what can you do",    "/help"),
]


def _nlu_expand(text: str) -> str:
    """
    Check if text (lowercased) matches a known synonym phrase.
    Returns the canonical slash-command string if matched, else original text.
    Preserves any trailing args after the matched phrase.
    """
    lower = text.lower().strip()
    for phrase, cmd in _NLU_SYNONYMS:
        if lower == phrase or lower.startswith(phrase + " ") or lower.startswith(phrase + ","):
            # Preserve anything after the matched phrase as args
            remainder = text[len(phrase):].strip()
            return f"{cmd} {remainder}".strip() if remainder else cmd
    return text


# ── Parsers ───────────────────────────────────────────────────────────────────
async def _parse_telegram(update: Dict[str, Any]) -> OrchestratorMessage:
    # BUG-NEW-1: edited_message events are re-edits of past msgs ? skip them.
    if "edited_message" in update and "message" not in update:
        print("[L1] Skipping edited_message ? not a new command")
        return OrchestratorMessage(
            text="",
            chat_id=update["edited_message"].get("chat", {}).get("id", 0),
            user=update["edited_message"].get("from", {}).get("username", "unknown"),
            source="telegram",
            message_type="edited",
            route="skip",
        )

    message = update.get("message", {}) or {}
    text = (
        message.get("text", "")
        or message.get("caption", "")
        or ""
    )

    # NLU expansion: map natural phrases → slash-commands before routing
    if text and not text.startswith("/"):
        expanded = _nlu_expand(text)
        if expanded != text:
            print(f"[L1] NLU expand: {text!r} → {expanded!r}")
            text = expanded

    msg = OrchestratorMessage(
        text=text,
        chat_id=message.get("chat", {}).get("id", 0),
        user=message.get("from", {}).get("username", "unknown"),
        source="telegram",
        message_type="message",
        route="conversation",
    )

    # Route detection
    if text.startswith("/"):
        msg.message_type = "command"
        msg.route = "command"
        # Extract command name for downstream use
        cmd = text.split()[0].lower().split("@")[0]  # strip @botname suffix
        msg.context["command"] = cmd
        msg.context["command_args"] = text[len(cmd):].strip()

    # Attachments
    for kind in ("photo", "document", "voice", "audio", "video", "sticker"):
        if kind in message:
            msg.attachments.append({"type": kind, "data": message[kind]})
            if kind == "voice":
                msg.message_type = "voice"
                # GAP-NEW-3: voice messages ? set text to a placeholder so pipeline
                # can respond informatively rather than silently dropping
                if not msg.text:
                    msg.text = "[voice message received]"
                    msg.context["voice_unsupported"] = True

    # Store raw Telegram message_id for potential reply threading
    msg.context["telegram_message_id"] = message.get("message_id")

    return msg


async def _parse_mcp(request: Dict[str, Any]) -> OrchestratorMessage:
    """MCP tool-call from Claude Desktop → always owner tier, always command route."""
    params = request.get("params", {})
    query = params.get("query", "") or params.get("text", "") or str(params)

    msg = OrchestratorMessage(
        text=query,
        chat_id=int(os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0"))),
        user="claude_desktop",
        source="mcp",
        message_type="command",
        route="command",
    )
    msg.context["mcp_method"] = request.get("method", "")
    msg.context["mcp_params"] = params
    return msg


async def _parse_system(event: Dict[str, Any]) -> OrchestratorMessage:
    """Background system events (cron, heartbeat, watchdog)."""
    msg = OrchestratorMessage(
        text=event.get("event_type", "system_event"),
        chat_id=int(os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0"))),
        user="system",
        source="system",
        message_type="event",
        route="background",
    )
    msg.context["event_data"] = event.get("data", {})
    return msg


# ── Entry point ───────────────────────────────────────────────────────────────
async def layer_1_triage(
    raw_input: Dict[str, Any],
    input_type: str = "telegram",
) -> OrchestratorMessage:
    """
    Parse raw input → OrchestratorMessage → run through L0 gate → hand to L2.
    Always returns an OrchestratorMessage (even on error, for traceability).
    """
    print(f"[L1] Received {input_type} input")

    try:
        if input_type == "telegram":
            msg = await _parse_telegram(raw_input)
            # GAP-NEW-1: Dedup ? reject Telegram webhook retries
            tg_message_id = raw_input.get("message", {}).get("message_id")
            if tg_message_id and _is_duplicate(tg_message_id):
                print(f"[L1] DEDUP drop ? message_id={tg_message_id} already processed")
                return msg
            # GAP-NEW-2: Typing indicator ? fire immediately, before any await
            if msg.route != "skip" and msg.chat_id:
                asyncio.ensure_future(_send_typing(msg.chat_id))
        elif input_type == "mcp":
            msg = await _parse_mcp(raw_input)
        elif input_type == "system":
            msg = await _parse_system(raw_input)
        else:
            raise ValueError(f"Unknown input_type: {input_type!r}")

        msg.track_layer("L1-PARSE")
        print(f"[L1] Parsed  type={msg.message_type}  route={msg.route}  user=@{msg.user}  text={msg.text[:60]!r}")

        # ── L0 security gate ──────────────────────────────────────────────────
        from core_orch_layer0 import gate_check
        if not gate_check(msg):
            print(f"[L1] L0 gate REJECTED — surfacing to output")
            from core_orch_layer10 import layer_10_output
            await layer_10_output(msg)
            return msg

        # ── Hand to L2 ────────────────────────────────────────────────────────
        from core_orch_layer2 import layer_2_memory
        await layer_2_memory(msg)
        return msg

    except Exception as exc:
        print(f"[L1] FATAL parse error: {exc}")
        err_msg = OrchestratorMessage(
            text=str(raw_input)[:200],
            chat_id=int(os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0"))),
            user="parse_error",
        )
        err_msg.add_error("L1", exc, "PARSE_ERROR")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(err_msg)
        return err_msg
