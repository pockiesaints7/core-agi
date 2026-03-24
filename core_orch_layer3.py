"""
core_orch_layer3.py — L3: Intent Classification
Uses gemini chat to classify user intent.
No mocks.
"""
import json
from typing import Any, Dict

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_FAST, GROQ_MODEL, gemini_chat

# Slash-commands that always map to specific intents without Groq
_COMMAND_INTENT_MAP = {
    "/health":      ("system_health",   True),
    "/state":       ("system_state",    True),
    "/status":      ("system_state",    True),
    "/tasks":       ("task_list",       True),
    "/evolutions":  ("evolution_list",  True),
    "/kb":          ("kb_search",       True),
    "/mistakes":    ("mistake_list",    True),
    "/train":       ("trigger_training",True),
    "/cold":        ("trigger_cold",    True),
    "/deploy":      ("deploy_status",   True),
    "/listen":      ("listen_mode",     True),
    "/checkpoint":  ("checkpoint",      True),
    "/help":        ("help",            False),
}

# ── L3 Fuzzy Intent Clusters ──────────────────────────────────────────────────
# Keyword sets per intent. If text contains ANY keyword from a cluster,
# that intent fires — no LLM call needed. Check BEFORE Gemini.
# Order matters: first cluster match wins.
_FUZZY_INTENT_CLUSTERS: list[tuple[str, bool, set[str]]] = [
    # (intent_name, requires_tools, keyword_set)
    ("system_health",    True,  {"health", "healthy", "alive", "ping", "heartbeat", "up?", "running ok", "online"}),
    ("system_state",     True,  {"state", "status", "running", "active", "what's up", "current state", "system info"}),
    ("task_list",        True,  {"tasks", "task list", "open tasks", "pending tasks", "my tasks", "todos", "task queue"}),
    ("evolution_list",   True,  {"evolution", "evolutions", "pending evo", "evolution queue", "suggested changes"}),
    ("kb_search",        True,  {"knowledge", "kb", "search kb", "what do you know", "find in kb", "knowledge base"}),
    ("mistake_list",     True,  {"mistakes", "errors", "mistake log", "what went wrong", "past errors", "error log"}),
    ("trigger_training", True,  {"train", "training", "run training", "start training", "trigger train"}),
    ("trigger_cold",     True,  {"cold", "cold processor", "cold run", "cold cycle", "run cold"}),
    ("deploy_status",    True,  {"deploy", "deployment", "redeploy", "build", "railway", "vm status", "oracle vm"}),
    ("kb_query",         True,  {"search for", "find me", "look up", "what is", "explain", "tell me about", "lookup"}),
    ("conversation",     False, {"hi", "hello", "hey", "thanks", "thank you", "ok", "cool", "got it", "nice", "good"}),
]

_CLASSIFY_SYSTEM = (
    "You are CORE AGI's intent classifier. Analyse the message and return ONLY valid JSON. "
    "No preamble, no markdown, no extra keys."
)

_CLASSIFY_TEMPLATE = """
MESSAGE: {text}
SOURCE: {source}
TYPE: {message_type}
TIER: {tier}
DOMAIN_CONTEXT: {domain}
BEHAVIORAL_RULES_COUNT: {rules_count}
KNOWN_MISTAKES_COUNT: {mistakes_count}

Classify this message. Return JSON only:
{{
  "intent": "task_execution|system_command|kb_query|general_query|conversation|greeting|error_recovery",
  "confidence": 0.0-1.0,
  "category": "task|question|command|conversation",
  "requires_tools": true|false,
  "tool_hints": ["tool_name_1", "tool_name_2"],
  "suggested_response_type": "conversational|structured|confirmation|error",
  "domain": "general|code|db|bot|mcp|training|kb|core_agi.patching"
}}
"""


def _fuzzy_match(text: str) -> Dict[str, Any]:
    """
    Scan text for keyword clusters. Returns classification dict if matched, else {}.
    Case-insensitive. Checks full text for any cluster keyword.
    """
    lower = text.lower()
    for intent, requires_tools, keywords in _FUZZY_INTENT_CLUSTERS:
        for kw in keywords:
            if kw in lower:
                return {
                    "intent": intent,
                    "confidence": 0.85,
                    "category": "command" if requires_tools else "conversation",
                    "requires_tools": requires_tools,
                    "tool_hints": [],
                    "suggested_response_type": "structured" if requires_tools else "conversational",
                    "domain": "general",
                    "_source": f"fuzzy:{kw}",
                }
    return {}


async def _fast_classify(msg: OrchestratorMessage) -> Dict[str, Any]:
    """Try deterministic classification before hitting Gemini."""

    # Slash-command fast path
    cmd = msg.context.get("command", "")
    if cmd and cmd in _COMMAND_INTENT_MAP:
        intent, needs_tools = _COMMAND_INTENT_MAP[cmd]
        return {
            "intent": intent,
            "confidence": 1.0,
            "category": "command",
            "requires_tools": needs_tools,
            "tool_hints": [],
            "suggested_response_type": "structured",
            "domain": msg.context.get("current_domain", "general"),
        }

    # Very short greetings
    text_lower = msg.text.lower().strip()
    if len(text_lower) <= 20 and any(
        g in text_lower for g in ("hi", "hello", "hey", "sup", "yo")
    ):
        return {
            "intent": "greeting",
            "confidence": 0.95,
            "category": "conversation",
            "requires_tools": False,
            "tool_hints": [],
            "suggested_response_type": "conversational",
            "domain": "general",
        }

    # Fuzzy keyword cluster matching (before Gemini)
    fuzzy = _fuzzy_match(msg.text)
    if fuzzy:
        src = fuzzy.pop("_source", "fuzzy")
        print(f"[L3] Fuzzy match ({src}) → intent={fuzzy['intent']}")
        fuzzy["domain"] = msg.context.get("current_domain", "general")
        return fuzzy

    return {}  # fall through to Gemini


async def layer_3_classify(msg: OrchestratorMessage):
    """
    Classify user intent using deterministic rules first, Gemini as fallback.
    Mutates msg.intent and msg.context['intent_classification'].
    """
    msg.track_layer("L3-START")
    print(f"[L3] Classifying intent …")

    # 1. Fast deterministic path (slash-cmd → greeting → fuzzy clusters)
    classification = await _fast_classify(msg)

    # 2. Gemini classification (only if no fast-path hit)
    if not classification:
        try:
            prompt = _CLASSIFY_TEMPLATE.format(
                text=msg.text[:500],
                source=msg.source,
                message_type=msg.message_type,
                tier=msg.tier,
                domain=msg.context.get("current_domain", "general"),
                rules_count=len(msg.context.get("behavioral_rules", [])),
                mistakes_count=len(msg.context.get("domain_mistakes", [])),
            )
            raw = gemini_chat(
                system=_CLASSIFY_SYSTEM,
                user=prompt,
                max_tokens=256,
                json_mode=True,
            )
            classification = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        except Exception as exc:
            print(f"[L3] Gemini classification failed (non-fatal): {exc}")
            classification = {
                "intent": "general_query",
                "confidence": 0.5,
                "category": "conversation",
                "requires_tools": False,
                "tool_hints": [],
                "suggested_response_type": "conversational",
                "domain": msg.context.get("current_domain", "general"),
            }

    msg.intent = classification.get("intent", "general_query")
    msg.context["intent_classification"] = classification

    msg.track_layer("L3-COMPLETE")
    print(
        f"[L3] intent={msg.intent}  conf={classification.get('confidence',0):.2f}"
        f"  tools={classification.get('requires_tools')}"
    )

    from core_orch_layer4 import layer_4_reason
    await layer_4_reason(msg)
