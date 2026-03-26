"""
core_worker_reflect.py — Worker 3: Gap Reflector
=================================================
Given a critique (weak/fail), identifies the knowledge gap that caused it.
Proposes new KB entries, behaviors, and mistakes to prevent recurrence.
Called by: core_orch_layer11.py (after critic, feeds meta evaluator)
"""
import json
from datetime import datetime

from core_config import sb_post, gemini_chat

_REFLECT_SYSTEM = (
    "You are CORE's self-improvement engine. "
    "Given a failed or weak output and its critique, identify the root gap and propose fixes. "
    "Return ONLY valid JSON — no preamble, no markdown."
)

_REFLECT_PROMPT = """
CORE produced a {verdict} output. Analyze the gap and propose improvements.

SOURCE: {source}
OUTPUT:
{output_text}

CRITIQUE:
- Score: {score}
- Failure pattern: {failure_pattern}
- Failure category: {failure_category}
- Reason: {reason}

Identify:
1. What knowledge gap caused this failure?
2. What new KB entry would prevent it?
3. What new behavioral rule would prevent it?
4. What mistake should be logged?

Return JSON only:
{{
  "gap": "<what CORE didn't know or misunderstood>",
  "gap_domain": "<which domain this gap belongs to>",
  "kb_entry": {{
    "topic": "<topic string>",
    "instruction": "<the rule or knowledge to store>",
    "domain": "<domain>",
    "confidence": "<high|medium|low>"
  }},
  "new_behavior": "<one actionable behavioral rule to add>",
  "mistake_entry": {{
    "what_failed": "<what specifically failed>",
    "correct_approach": "<what should have been done>",
    "severity": "<low|medium|high>",
    "root_cause": "<one sentence root cause>"
  }},
  "evo_worthy": <true if this pattern is serious enough to queue an evolution, else false>
}}
"""

_PROMPT_REFLECT_PROMPT = """
CORE's system prompt for {target} scored {score} ({verdict}).

PROMPT CONTENT:
{output_text}

CRITIQUE:
- Failure pattern: {failure_pattern}
- Suggested improvement: {suggested_improvement}

Propose improvements to make this prompt more effective.

Return JSON only:
{{
  "gap": "<what the prompt fails to cover or handle>",
  "gap_domain": "system_prompt",
  "kb_entry": {{
    "topic": "prompt_improvement_{target}",
    "instruction": "<what to add or change in this prompt>",
    "domain": "meta",
    "confidence": "high"
  }},
  "new_behavior": "<one behavioral rule related to this prompt weakness>",
  "mistake_entry": {{
    "what_failed": "<prompt weakness>",
    "correct_approach": "<improved prompt direction>",
    "severity": "medium",
    "root_cause": "<why the prompt is weak here>"
  }},
  "evo_worthy": true,
  "prompt_patch": "<the exact text addition or replacement suggested for the prompt>"
}}
"""


async def reflect_on_gaps(
    output_text: str,
    critique: dict,
    source: str = "session",
    session_id: str = "",
    prompt_target: str = "",
) -> dict:
    """
    Reflect on a weak/fail critique. Extracts gap, proposes KB + behavior + mistake.
    Only runs if verdict != 'ok'.
    Returns reflection dict for meta evaluator.
    """
    verdict = critique.get("verdict", "ok")
    if verdict == "ok":
        return {"ok": True, "skipped": True, "reason": "verdict=ok, no gap to extract"}

    text_trimmed = output_text.strip()[:2000]

    try:
        if source == "system_prompt":
            prompt = _PROMPT_REFLECT_PROMPT.format(
                target=prompt_target or "unknown",
                score=critique.get("score", 0),
                verdict=verdict,
                output_text=text_trimmed,
                failure_pattern=critique.get("failure_pattern") or "none",
                suggested_improvement=critique.get("suggested_improvement") or "none",
            )
        else:
            prompt = _REFLECT_PROMPT.format(
                verdict=verdict,
                source=source,
                output_text=text_trimmed,
                score=critique.get("score", 0),
                failure_pattern=critique.get("failure_pattern") or "none",
                failure_category=critique.get("failure_category") or "none",
                reason=critique.get("reason") or "none",
            )

        raw = gemini_chat(
            system=_REFLECT_SYSTEM,
            user=prompt,
            max_tokens=700,
            json_mode=True,
        )
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

    except Exception as e:
        print(f"[REFLECT] Gemini reflection failed: {e}")
        return {"ok": False, "error": str(e)}

    # Store reflection
    row = {
        "session_id":    session_id or None,
        "source":        source,
        "critique_score": float(critique.get("score", 0)),
        "verdict":       verdict,
        "gap":           (result.get("gap") or "")[:500],
        "gap_domain":    result.get("gap_domain", "general"),
        "new_behavior":  (result.get("new_behavior") or "")[:500],
        "evo_worthy":    bool(result.get("evo_worthy", False)),
        "prompt_patch":  (result.get("prompt_patch") or None),
        "created_at":    datetime.utcnow().isoformat(),
    }
    sb_post("output_reflections", row)

    print(f"[REFLECT] gap='{row['gap'][:60]}' evo_worthy={row['evo_worthy']}")

    return {
        "ok":          True,
        "gap":         row["gap"],
        "gap_domain":  row["gap_domain"],
        "kb_entry":    result.get("kb_entry"),
        "new_behavior": result.get("new_behavior"),
        "mistake_entry": result.get("mistake_entry"),
        "evo_worthy":  row["evo_worthy"],
        "prompt_patch": row["prompt_patch"],
    }
