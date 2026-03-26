"""
core_meta_evaluator.py — Meta Evaluator
========================================
Receives critique + reflection. Decides what action to take.
Deduplicates by pattern. Escalates recurring failures to Evo queue.
Called by: core_orch_layer11.py (after critic + reflect)
"""
import hashlib
import json
from datetime import datetime

from core_config import sb_post, sb_get, sb_patch, gemini_chat
from core_embeddings import _embed_text_safe

# Thresholds
_FREQ_REINFORCE   = 2   # seen N times → reinforce importance
_FREQ_ESCALATE    = 4   # seen N times → push to evo queue
_SIM_THRESHOLD    = 0.88  # cosine similarity to consider patterns "same"


def _find_similar_pattern(pattern_text: str) -> dict | None:
    """
    Semantic dedup: check if this pattern already exists in meta_decisions.
    Uses embedding similarity via pgvector. Falls back to hash match.
    Returns existing row or None.
    """
    if not pattern_text:
        return None

    # Fast path: exact hash match
    pattern_hash = hashlib.md5(pattern_text.encode()).hexdigest()
    try:
        rows = sb_get(
            "meta_decisions",
            f"select=id,pattern_text,frequency,action,escalated"
            f"&pattern_hash=eq.{pattern_hash}&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        if rows:
            return rows[0]
    except Exception:
        pass

    # Semantic path: embedding similarity
    try:
        vec = _embed_text_safe(pattern_text)
        if not vec:
            return None
        import httpx
        from core_config import SUPABASE_URL, _sbh
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/rpc/match_meta_patterns",
            headers={**_sbh(True), "Prefer": "return=representation"},
            json={
                "query_embedding": vec,
                "match_threshold": _SIM_THRESHOLD,
                "match_count": 1,
            },
            timeout=10,
        )
        if r.is_success and r.json():
            return r.json()[0]
    except Exception as e:
        print(f"[META] semantic dedup failed: {e}")

    return None


def _store_kb_entry(kb: dict, source: str) -> bool:
    """Write a new KB entry and trigger embedding."""
    if not kb or not kb.get("instruction"):
        return False
    try:
        row = {
            "topic":       kb.get("topic", "auto_generated"),
            "instruction": kb["instruction"][:800],
            "domain":      kb.get("domain", "general"),
            "confidence":  kb.get("confidence", "medium"),
            "active":      True,
            "source":      f"worker_auto|{source}",
            "created_at":  datetime.utcnow().isoformat(),
        }
        ok = sb_post("knowledge_base", row)
        if ok:
            # Trigger embedding async (best effort)
            try:
                rows = sb_get(
                    "knowledge_base",
                    f"select=id&topic=eq.{kb.get('topic','')}"
                    f"&order=created_at.desc&limit=1",
                    svc=True,
                ) or []
                if rows:
                    from core_embeddings import t_embed_kb_entry
                    t_embed_kb_entry(str(rows[0]["id"]))
            except Exception as ee:
                print(f"[META] embed kb entry failed (non-fatal): {ee}")
        return ok
    except Exception as e:
        print(f"[META] store_kb_entry failed: {e}")
        return False


def _store_mistake(mistake: dict, source: str) -> bool:
    """Write a mistake entry."""
    if not mistake or not mistake.get("what_failed"):
        return False
    try:
        row = {
            "what_failed":      mistake["what_failed"][:400],
            "correct_approach": (mistake.get("correct_approach") or "")[:400],
            "severity":         mistake.get("severity", "medium"),
            "root_cause":       (mistake.get("root_cause") or "")[:300],
            "domain":           "auto_detected",
            "source":           f"worker_auto|{source}",
            "created_at":       datetime.utcnow().isoformat(),
        }
        return sb_post("domain_mistakes", row)
    except Exception as e:
        print(f"[META] store_mistake failed: {e}")
        return False


def _push_evo_queue(pattern: str, reflection: dict, source: str) -> bool:
    """Push a recurring failure to evolution_queue for review."""
    if not pattern:
        return False
    try:
        behavior = reflection.get("new_behavior") or pattern
        gap      = reflection.get("gap") or pattern
        row = {
            "change_type":    "behavior",
            "change_summary": f"[AUTO] Recurring failure: {pattern[:200]}",
            "diff_content":   json.dumps({
                "gap":          gap,
                "new_behavior": behavior,
                "source":       source,
                "prompt_patch": reflection.get("prompt_patch"),
            }),
            "confidence":     0.75,
            "pattern_key":    hashlib.md5(pattern.encode()).hexdigest(),
            "status":         "pending",
            "auto_generated": True,
            "created_at":     datetime.utcnow().isoformat(),
        }
        return sb_post("evolution_queue", row)
    except Exception as e:
        print(f"[META] push_evo_queue failed: {e}")
        return False


async def evaluate(critique: dict, reflection: dict, source: str = "session") -> dict:
    """
    Core meta decision engine.
    Deduplicates patterns, decides action, writes meta_decisions row.

    Actions:
      ignore    — verdict=ok, nothing to do
      add_kb    — new pattern, store KB + mistake
      reinforce — seen before but < escalation threshold, bump frequency
      add_evo   — recurring (freq >= _FREQ_ESCALATE), push to evo queue
    """
    verdict = critique.get("verdict", "ok")
    pattern = critique.get("failure_pattern")

    if verdict == "ok" or not pattern:
        return {"ok": True, "action": "ignore", "reason": "verdict=ok"}

    pattern_hash = hashlib.md5(pattern.encode()).hexdigest()
    existing     = _find_similar_pattern(pattern)

    if not existing:
        # Brand new pattern
        action    = "add_kb"
        frequency = 1

        _store_kb_entry(reflection.get("kb_entry"), source)
        _store_mistake(reflection.get("mistake_entry"), source)

        # Embed the pattern for future semantic dedup
        vec = _embed_text_safe(pattern)
        row = {
            "pattern_text":     pattern[:500],
            "pattern_hash":     pattern_hash,
            "failure_category": critique.get("failure_category", "none"),
            "source":           source,
            "action":           action,
            "frequency":        frequency,
            "escalated":        False,
            "embedding":        vec if vec else None,
            "created_at":       datetime.utcnow().isoformat(),
        }
        sb_post("meta_decisions", row)
        print(f"[META] NEW pattern → add_kb | source={source}")

    else:
        freq = int(existing.get("frequency", 1)) + 1

        if freq >= _FREQ_ESCALATE and not existing.get("escalated"):
            action = "add_evo"
            _push_evo_queue(pattern, reflection, source)
            escalated = True
            print(f"[META] RECURRING (x{freq}) → add_evo | pattern='{pattern[:60]}'")
        elif freq >= _FREQ_REINFORCE:
            action    = "reinforce"
            escalated = existing.get("escalated", False)
            print(f"[META] SEEN (x{freq}) → reinforce | pattern='{pattern[:60]}'")
        else:
            action    = "reinforce"
            escalated = existing.get("escalated", False)

        # Update frequency + action
        try:
            sb_patch(
                "meta_decisions",
                f"id=eq.{existing['id']}",
                {
                    "frequency": freq,
                    "action":    action,
                    "escalated": escalated,
                    "last_seen": datetime.utcnow().isoformat(),
                },
            )
        except Exception as e:
            print(f"[META] frequency update failed: {e}")

    # Handle system_prompt evolution separately
    if source == "system_prompt" and reflection.get("prompt_patch"):
        _handle_prompt_evolution(reflection, critique)

    return {
        "ok":      True,
        "action":  action,
        "pattern": pattern[:100],
        "source":  source,
    }


def _handle_prompt_evolution(reflection: dict, critique: dict) -> None:
    """
    When a system prompt is critiqued, queue a prompt patch as an evolution.
    The patch is stored for human review before being applied.
    """
    patch = reflection.get("prompt_patch")
    if not patch:
        return
    try:
        row = {
            "change_type":    "system_prompt",
            "change_summary": f"[AUTO] Prompt improvement: {reflection.get('gap','')[:150]}",
            "diff_content":   json.dumps({
                "prompt_target":     critique.get("prompt_target", "unknown"),
                "prompt_version":    critique.get("prompt_version", 0),
                "patch":             patch,
                "suggested_improve": critique.get("suggested_improvement", ""),
            }),
            "confidence":     float(critique.get("score", 0.5)),
            "pattern_key":    f"prompt_{critique.get('prompt_target','unknown')}",
            "status":         "pending",
            "auto_generated": True,
            "created_at":     datetime.utcnow().isoformat(),
        }
        sb_post("evolution_queue", row)
        print(f"[META] Prompt evolution queued for target={critique.get('prompt_target')}")
    except Exception as e:
        print(f"[META] prompt evolution queue failed: {e}")
