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

# Known slash-commands that require tool execution
_COMMAND_ROUTES = {
    "/health", "/state", "/status", "/tasks", "/evolutions",
    "/kb", "/mistakes", "/train", "/cold", "/deploy",
    "/listen", "/checkpoint", "/help",
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
    message = update.get("message", {}) or update.get("edited_message", {})
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
