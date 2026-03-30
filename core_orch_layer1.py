"""
core_orch_layer1.py â€” L1: Input Reception & Triage
Parses raw Telegram/MCP/system payloads into OrchestratorMessage.
Entry point for all traffic.
"""
import os
import asyncio
from typing import Dict, Any
try:
    try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False
except Exception:
    def load_dotenv(*args, **kwargs):
        return False
from orchestrator_message import OrchestratorMessage
from core_orch_context import initial_request_profile

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

# â”€â”€ L1 NLU Synonym Expansion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Maps natural-language phrases â†’ canonical slash-commands.
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
    # time
    ("what time is it",    "/time"),
    ("what time is now",   "/time"),
    ("current time",       "/time"),
    ("what's the time",    "/time"),
    ("what day is it",     "/time"),
    ("what date is it",    "/time"),
    ("time now",           "/time"),
    # calc â€” only match when clearly followed by a math expression (trailing space required)
    # DO NOT use bare "what is" â€” too greedy, matches "what is your health" etc.
    # weather
    ("what's the weather", "/weather"),
    ("weather today",      "/weather"),
    ("weather in",         "/weather"),
    # tools
    ("list tools",         "/tools"),
    ("show tools",         "/tools"),
    ("available tools",    "/tools"),
    ("what tools do you have", "/tools"),
    ("how many tools",     "/tools"),
    ("name your tools",    "/tools"),
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


# â”€â”€ Parsers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # NLU expansion: map natural phrases â†’ slash-commands before routing
    if text and not text.startswith("/"):
        expanded = _nlu_expand(text)
        if expanded != text:
            print(f"[L1] NLU expand: {text!r} â†’ {expanded!r}")
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

    # Initial request profile (intent unknown yet)
    try:
        profile = initial_request_profile(msg)
        msg.input_profile = profile.get("input_profile", {})
        msg.speech_act_packet = profile.get("speech_act_packet", {})
        msg.task_mode_packet = profile.get("task_mode_packet", {})
        msg.request_kind = profile.get("request_kind", "")
        msg.response_mode = profile.get("response_mode", "")
        msg.route_reason = profile.get("route_reason", "")
        msg.clarification_needed = bool(profile.get("clarification_needed", False))
        msg.context["input_profile"] = msg.input_profile
        msg.context["speech_act_packet"] = msg.speech_act_packet
        msg.context["task_mode_packet"] = msg.task_mode_packet
        msg.context["request_profile"] = profile
        msg.context["request_kind"] = msg.request_kind
        msg.context["response_mode"] = msg.response_mode
        msg.context["route_reason"] = msg.route_reason
        msg.context["clarification_needed"] = msg.clarification_needed
    except Exception as exc:
        print(f"[L1] request_profile init failed (non-fatal): {exc}")

    return msg


async def _parse_mcp(request: Dict[str, Any]) -> OrchestratorMessage:
    """MCP tool-call from Claude Desktop â†’ always owner tier, always command route."""
    params = request.get("params", {})
    query = params.get("query", "") or params.get("text", "") or str(params)

    msg = OrchestratorMessage(
        text=query,
        chat_id=_env_int("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0")),
        user="claude_desktop",
        source="mcp",
        message_type="command",
        route="command",
    )
    msg.context["mcp_method"] = request.get("method", "")
    msg.context["mcp_params"] = params

    # Initial request profile for MCP
    try:
        profile = initial_request_profile(msg)
        msg.input_profile = profile.get("input_profile", {})
        msg.speech_act_packet = profile.get("speech_act_packet", {})
        msg.task_mode_packet = profile.get("task_mode_packet", {})
        msg.request_kind = profile.get("request_kind", "")
        msg.response_mode = profile.get("response_mode", "")
        msg.route_reason = profile.get("route_reason", "")
        msg.clarification_needed = bool(profile.get("clarification_needed", False))
        msg.context["input_profile"] = msg.input_profile
        msg.context["speech_act_packet"] = msg.speech_act_packet
        msg.context["task_mode_packet"] = msg.task_mode_packet
        msg.context["request_profile"] = profile
        msg.context["request_kind"] = msg.request_kind
        msg.context["response_mode"] = msg.response_mode
        msg.context["route_reason"] = msg.route_reason
        msg.context["clarification_needed"] = msg.clarification_needed
    except Exception as exc:
        print(f"[L1] request_profile init failed (non-fatal): {exc}")
    return msg


async def _parse_system(event: Dict[str, Any]) -> OrchestratorMessage:
    """Background system events (cron, heartbeat, watchdog)."""
    msg = OrchestratorMessage(
        text=event.get("event_type", "system_event"),
        chat_id=_env_int("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0")),
        user="system",
        source="system",
        message_type="event",
        route="background",
    )
    msg.context["event_data"] = event.get("data", {})

    try:
        profile = initial_request_profile(msg)
        msg.input_profile = profile.get("input_profile", {})
        msg.speech_act_packet = profile.get("speech_act_packet", {})
        msg.task_mode_packet = profile.get("task_mode_packet", {})
        msg.request_kind = profile.get("request_kind", "")
        msg.response_mode = profile.get("response_mode", "")
        msg.route_reason = profile.get("route_reason", "")
        msg.clarification_needed = bool(profile.get("clarification_needed", False))
        msg.context["input_profile"] = msg.input_profile
        msg.context["speech_act_packet"] = msg.speech_act_packet
        msg.context["task_mode_packet"] = msg.task_mode_packet
        msg.context["request_profile"] = profile
        msg.context["request_kind"] = msg.request_kind
        msg.context["response_mode"] = msg.response_mode
        msg.context["route_reason"] = msg.route_reason
        msg.context["clarification_needed"] = msg.clarification_needed
    except Exception as exc:
        print(f"[L1] request_profile init failed (non-fatal): {exc}")
    return msg


# â”€â”€ Entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def layer_1_triage(
    raw_input: Dict[str, Any],
    input_type: str = "telegram",
) -> OrchestratorMessage:
    """
    Parse raw input â†’ OrchestratorMessage â†’ run through L0 gate â†’ hand to L2.
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

        # â”€â”€ L0 security gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        from core_orch_layer0 import gate_check
        if not gate_check(msg):
            print(f"[L1] L0 gate REJECTED â€” surfacing to output")
            from core_orch_layer10 import layer_10_output
            await layer_10_output(msg)
            return msg

        # â”€â”€ Hand to L2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        from core_orch_layer2 import layer_2_memory
        await layer_2_memory(msg)
        return msg

    except Exception as exc:
        import traceback
        print(f"[L1] FATAL parse error: {exc}")
        print(f"[L1] Traceback:\n{traceback.format_exc()}")
        err_msg = OrchestratorMessage(
            text=str(raw_input)[:200],
            chat_id=_env_int("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "0")),
            user="parse_error",
        )
        err_msg.add_error("L1", exc, "PARSE_ERROR")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(err_msg)
        return err_msg



