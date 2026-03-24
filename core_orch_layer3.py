"""
core_orch_layer3.py — L3: Intent Classification
Uses real Groq (GROQ_FAST model) to classify user intent.
No mocks.
"""
import json
from typing import Any, Dict

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_FAST

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


async def _fast_classify(msg: OrchestratorMessage) -> Dict[str, Any]:
    """Try deterministic classification before hitting Groq."""

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

    return {}  # fall through to Groq


async def layer_3_classify(msg: OrchestratorMessage):
    """
    Classify user intent using deterministic rules first, Groq as fallback.
    Mutates msg.intent and msg.context['intent_classification'].
    """
    msg.track_layer("L3-START")
    print(f"[L3] Classifying intent …")

    # 1. Fast deterministic path
    classification = await _fast_classify(msg)

    # 2. Groq classification
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
            raw = groq_chat(
                system=_CLASSIFY_SYSTEM,
                user=prompt,
                model=GROQ_FAST,
                max_tokens=256,
            )
            classification = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        except Exception as exc:
            print(f"[L3] Groq classification failed (non-fatal): {exc}")
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
