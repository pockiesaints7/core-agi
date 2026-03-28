"""
core_worker_causal.py — Worker 2: Causal Chain Extractor
=========================================================
Asks WHY for every CORE output. Traces reasoning backwards.
Builds a causal map of CORE's own thinking over time.
Called by: core_orch_layer11.py (parallel with critic)
"""
import json
from datetime import datetime

from core_config import sb_post, gemini_chat

_CAUSAL_SYSTEM = (
    "You are CORE's causal reasoning analyst. "
    "Given an output, trace backwards: what knowledge, rules, or patterns caused this response? "
    "Return ONLY valid JSON — no preamble, no markdown."
)

_CAUSAL_PROMPT = """
Given this CORE output, ask: WHY was this the answer?

SOURCE: {source}
OUTPUT:
{output_text}

Trace the reasoning chain backwards. What knowledge/pattern/rule drove this?

Return JSON only:
{{
  "why_reasoning": "<step-by-step backward trace of what caused this output>",
  "root_knowledge": "<the core knowledge or rule that most influenced this output>",
  "knowledge_source": "<kb_entry|behavioral_rule|training|unknown>",
  "reasoning_type": "<lookup|inference|pattern_match|calculation|retrieval|generation>",
  "confidence": <float 0.0-1.0>,
  "potential_bias": "<any bias or assumption that may have skewed this output, or null>"
}}
"""


async def extract_causality(
    output_text: str,
    source: str = "session",
    session_id: str = "",
    context: dict = None,
) -> dict:
    """
    Extract causal chain from a CORE output. Stores in causal_chains table.
    Runs async, non-blocking.
    """
    if not output_text or len(output_text.strip()) < 10:
        return {"ok": False, "error": "output_text too short"}

    text_trimmed = output_text.strip()[:3000]

    try:
        prompt = _CAUSAL_PROMPT.format(
            source=source,
            output_text=text_trimmed,
        )
        raw = gemini_chat(
            system=_CAUSAL_SYSTEM,
            user=prompt,
            max_tokens=600,
            json_mode=True,
        )
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

    except Exception as e:
        print(f"[CAUSAL] Gemini causal extraction failed: {e}")
        result = {
            "why_reasoning": f"Causal extraction failed: {str(e)[:100]}",
            "root_knowledge": "unknown",
            "knowledge_source": "unknown",
            "reasoning_type": "unknown",
            "confidence": 0.0,
            "potential_bias": None,
        }

    row = {
        "session_id":       session_id or None,
        "source":           source,
        "output_text":      text_trimmed[:1500],
        "why_reasoning":    (result.get("why_reasoning") or "")[:1000],
        "root_knowledge":   (result.get("root_knowledge") or "")[:500],
        "knowledge_source": result.get("knowledge_source", "unknown"),
        "reasoning_type":   result.get("reasoning_type", "unknown"),
        "confidence":       float(result.get("confidence", 0.0)),
        "potential_bias":   (result.get("potential_bias") or None),
        "created_at":       datetime.utcnow().isoformat(),
    }
    sb_post("causal_chains", row)

    print(f"[CAUSAL] source={source} reasoning_type={row['reasoning_type']} confidence={row['confidence']:.2f}")

    return {
        "ok":              True,
        "why_reasoning":   row["why_reasoning"],
        "root_knowledge":  row["root_knowledge"],
        "reasoning_type":  row["reasoning_type"],
        "confidence":      row["confidence"],
        "potential_bias":  row["potential_bias"],
    }
