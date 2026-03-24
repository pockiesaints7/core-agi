"""
core_orch_layer8.py — L8: Safety & Output Redaction
Scans all tool results and styled responses for secrets/PII
before anything reaches Telegram or MCP output.
"""
import re
from typing import Any, List, Tuple

from orchestrator_message import OrchestratorMessage

# Patterns are ordered most-specific → least-specific
_REDACTION_PATTERNS = [
    ("GITHUB_PAT",       re.compile(r"gh[ps]_[a-zA-Z0-9]{36,}")),
    ("JWT_TOKEN",        re.compile(
        r"eyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+"
    )),
    ("GROQ_API_KEY",     re.compile(r"gsk_[a-zA-Z0-9]{40,}")),
    ("TELEGRAM_TOKEN",   re.compile(r"\d{9,10}:[a-zA-Z0-9\-_]{35}")),
    ("SUPABASE_SVC_KEY", re.compile(r"sb-[a-zA-Z0-9]{32,}")),
    ("LOCAL_PATH",       re.compile(r"(?:/home|/root|/var|/opt)/[\w.\-/]+")),
    ("IP_ADDRESS",       re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|\d)\b(?!\w|-)")),  # BUG5: tightened, skips version strings
    ("EMAIL",            re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
]


def _redact(text: str, skip_labels: set = None) -> Tuple[str, List[str]]:
    """Return (redacted_text, [label, ...]) for all matched patterns."""
    redacted = text
    found: List[str] = []
    for label, pattern in _REDACTION_PATTERNS:
        if skip_labels and label in skip_labels:
            continue
        if pattern.search(redacted):
            found.append(label)
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted, found


# GAP-NEW-20: dict keys that are legitimate path outputs ? skip LOCAL_PATH redaction
_PATH_OUTPUT_KEYS = frozenset(["path","file","location","output","filename","filepath","dir","directory"])


def _deep_redact(obj: Any, skip_labels: set = None, _key: str = None) -> Tuple[Any, List[str]]:
    """Recursively redact strings inside dicts/lists.
    GAP-NEW-20: skip LOCAL_PATH on known path-output keys."""
    all_found: List[str] = []
    if isinstance(obj, str):
        effective_skip = set(skip_labels or set())
        if _key and _key.lower() in _PATH_OUTPUT_KEYS:
            effective_skip.add("LOCAL_PATH")
        clean, found = _redact(obj, skip_labels=effective_skip if effective_skip else None)
        return clean, found
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            cv, found = _deep_redact(v, skip_labels=skip_labels, _key=k)
            cleaned[k] = cv
            all_found.extend(found)
        return cleaned, all_found
    if isinstance(obj, list):
        cleaned_list = []
        for item in obj:
            ci, found = _deep_redact(item, skip_labels=skip_labels)
            cleaned_list.append(ci)
            all_found.extend(found)
        return cleaned_list, all_found
    return obj, []


# Patterns that should NOT be redacted for owner tier (not real secrets)
_OWNER_SAFE_LABELS = {"LOCAL_PATH", "IP_ADDRESS", "EMAIL"}


async def layer_8_safety(msg: OrchestratorMessage):
    """
    Scan tool results and styled response for secrets/PII.
    Redact in-place before output delivery.
    Owner tier: only redact actual secrets (tokens, keys, JWTs).
    """
    msg.track_layer("L8-START")
    is_owner = (msg.tier == "owner")

    skip_labels = _OWNER_SAFE_LABELS if is_owner else None

    # Redact tool results
    for result in msg.tool_results:
        raw = result.get("result")
        if raw is not None:
            clean, found = _deep_redact(raw, skip_labels=skip_labels)
            if found:
                result["result"] = clean
                msg.safety_redacted.extend(found)
                print(f"[L8] Redacted {found} from {result.get('tool','?')} result")

    # Redact styled response if already set
    if msg.styled_response:
        clean, found = _redact(msg.styled_response, skip_labels=skip_labels)
        if found:
            msg.styled_response = clean
            msg.safety_redacted.extend(found)
            print(f"[L8] Redacted {found} from styled_response")

    # GAP-NEW-12: surface preflight warnings into context so L9 can mention them
    preflight = msg.context.get("preflight_checks", {})
    pf_warnings = preflight.get("warnings", [])
    if pf_warnings:
        msg.context["preflight_warning_note"] = "?? " + " | ".join(pf_warnings[:3])
        print(f"[L8] Preflight warnings surfaced: {pf_warnings}")

    msg.track_layer("L8-COMPLETE")
    if msg.safety_redacted:
        print(f"[L8] Total redactions: {list(set(msg.safety_redacted))}")

    from core_orch_layer9 import layer_9_tone
    await layer_9_tone(msg)
