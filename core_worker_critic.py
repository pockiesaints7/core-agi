"""
core_worker_critic.py — Worker 1: Output Critic
================================================
Evaluates every CORE output (session, autonomous, background research, system prompt).
Scores quality, detects failure patterns, writes to output_critiques table.
Called by: core_orch_layer11.py
Feeds into: core_meta_evaluator.py
"""
import hashlib
import json
from datetime import datetime

from core_config import sb_post, sb_get, gemini_chat

_CRITIC_SYSTEM = (
    "You are CORE's strict quality critic. "
    "Evaluate outputs from an autonomous AGI system. "
    "Be precise, harsh but fair. Return ONLY valid JSON — no preamble, no markdown."
)

_CRITIC_PROMPT = """
Evaluate this CORE output:

SOURCE: {source}
OUTPUT:
{output_text}

Return JSON only:
{{
  "score": <float 0.0-1.0>,
  "verdict": "<ok|weak|fail>",
  "failure_pattern": "<one sentence describing the failure pattern, or null if verdict=ok>",
  "failure_category": "<hallucination|incomplete|wrong_tool|reasoning_gap|format_error|none>",
  "reason": "<brief explanation>",
  "strengths": "<what was done well, or null>"
}}

Scoring: 0.85-1.0=ok, 0.60-0.84=weak, 0.0-0.59=fail
"""

_PROMPT_CRITIC_PROMPT = """
Evaluate this CORE system prompt for quality and effectiveness:

TARGET: {target}
VERSION: {version}
CONTENT:
{output_text}

Return JSON only:
{{
  "score": <float 0.0-1.0>,
  "verdict": "<ok|weak|fail>",
  "failure_pattern": "<main weakness, or null if ok>",
  "failure_category": "<ambiguous|missing_coverage|too_verbose|misaligned|none>",
  "reason": "<brief explanation>",
  "suggested_improvement": "<one concrete improvement, or null>"
}}
"""


def critique_output(
    output_text: str,
    source: str = "session",
    session_id: str = "",
    context: dict = None,
    prompt_target: str = "",
    prompt_version: int = 0,
) -> dict:
    """
    Evaluate a CORE output. Stores result in output_critiques table.

    source: 'session' | 'autonomous' | 'background_research' | 'system_prompt'
    """
    if not output_text or len(output_text.strip()) < 10:
        return {"ok": False, "error": "output_text too short"}

    text_trimmed = output_text.strip()[:3000]

    try:
        if source == "system_prompt":
            prompt = _PROMPT_CRITIC_PROMPT.format(
                target=prompt_target or "unknown",
                version=prompt_version or 0,
                output_text=text_trimmed,
            )
        else:
            prompt = _CRITIC_PROMPT.format(
                source=source,
                output_text=text_trimmed,
            )

        raw = gemini_chat(
            system=_CRITIC_SYSTEM,
            user=prompt,
            max_tokens=500,
            json_mode=True,
        )
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

    except Exception as e:
        print(f"[CRITIC] Gemini critique failed: {e}")
        result = {
            "score": 0.5, "verdict": "weak",
            "failure_pattern": f"Critic failed: {str(e)[:100]}",
            "failure_category": "none", "reason": "critique_error", "strengths": None,
        }

    score   = float(result.get("score", 0.5))
    verdict = result.get("verdict", "weak")
    pattern = result.get("failure_pattern") or None
    cat     = result.get("failure_category", "none")

    content_hash = hashlib.md5(text_trimmed.encode()).hexdigest()
    pattern_hash = hashlib.md5((pattern or "").encode()).hexdigest() if pattern else None

    row = {
        "session_id":       session_id or None,
        "source":           source,
        "output_text":      text_trimmed[:2000],
        "score":            score,
        "verdict":          verdict,
        "failure_pattern":  pattern,
        "failure_category": cat,
        "reason":           (result.get("reason") or "")[:300],
        "strengths":        (result.get("strengths") or None),
        "content_hash":     content_hash,
        "pattern_hash":     pattern_hash,
        "prompt_target":    prompt_target or None,
        "prompt_version":   prompt_version or None,
        "suggested_improvement": (result.get("suggested_improvement") or None),
        "created_at":       datetime.utcnow().isoformat(),
    }
    sb_post("output_critiques", row)

    critique_id = None
    try:
        rows = sb_get(
            "output_critiques",
            f"select=id&content_hash=eq.{content_hash}&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        critique_id = rows[0]["id"] if rows else None
    except Exception:
        pass

    print(f"[CRITIC] source={source} verdict={verdict} score={score:.2f}")

    return {
        "ok": True,
        "critique_id":     critique_id,
        "score":           score,
        "verdict":         verdict,
        "failure_pattern": pattern,
        "failure_category": cat,
        "pattern_hash":    pattern_hash,
        "content_hash":    content_hash,
    }
