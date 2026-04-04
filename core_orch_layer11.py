"""
core_orch_layer11.py — L11: Self-Improvement Pipeline
======================================================
Fires AFTER every output, async, non-blocking.
Processes ALL output sources: session, autonomous, background_research, system_prompt, trading.

Wiring:
  Output sent → asyncio.create_task(layer11_post_output(...))
                    ↓ (parallel)
           Critic ──┬── Causal
                    ↓
                 Reflect
                    ↓
             Meta Evaluator
                    ↓
          KB | Mistake | Evo queue

Never blocks the main response pipeline.

Sources:
  session           — owner chat responses
  autonomous        — agentic task outputs
  background_research — cold processor outputs
  system_prompt     — prompt quality evaluation
  trading           — trade outcomes from core-trading-bot (Week 1 integration)
"""
import asyncio
import uuid
from datetime import datetime

from core_worker_critic  import critique_output
from core_worker_causal  import extract_causality
from core_worker_reflect import reflect_on_gaps
from core_meta_evaluator import evaluate
from core_reflection_audit import (
    build_reflection_context,
    finalize_reflection_event,
    note_reflection_stage,
    register_reflection_event,
)


def _effective_session_id(session_id: str = "", context: dict = None) -> str:
    if session_id:
        return session_id
    trace_id = (context or {}).get("trace_id")
    if not trace_id:
        return ""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(trace_id)))


async def layer11_post_output(
    output_text: str,
    source: str = "session",
    session_id: str = "",
    context: dict = None,
    prompt_target: str = "",
    prompt_version: int = 0,
) -> None:
    """
    Full self-improvement pipeline. Non-blocking — called via asyncio.create_task().

    Args:
        output_text:    The CORE output to process
        source:         'session' | 'autonomous' | 'background_research' | 'system_prompt' | 'trading'
        session_id:     Optional session UUID
        context:        Optional context dict (intent, domain, etc.)
        prompt_target:  If source='system_prompt', which prompt (e.g. 'background_researcher')
        prompt_version: If source='system_prompt', the version number
    """
    if not output_text or len(output_text.strip()) < 20:
        return

    print(f"[L11] Post-output pipeline firing | source={source}")
    effective_session_id = _effective_session_id(session_id, context)
    runtime_context = dict(context or {})
    runtime_context.setdefault("source", source)
    runtime_context.setdefault("source_domain", runtime_context.get("source_domain") or ("trading" if source == "trading" else "core"))
    runtime_context.setdefault("source_branch", runtime_context.get("source_branch") or source)
    runtime_context.setdefault("source_service", runtime_context.get("source_service") or "core-agi")
    if effective_session_id and not runtime_context.get("session_id"):
        runtime_context["session_id"] = effective_session_id

    event_id = runtime_context.get("event_id")
    if not event_id:
        seed_context = build_reflection_context(
            runtime_context,
            source_domain=runtime_context.get("source_domain") or ("trading" if source == "trading" else "core"),
            source_branch=runtime_context.get("source_branch") or source,
            source_service=runtime_context.get("source_service") or "core-agi",
            output_text=output_text,
            source=source,
        )
        ingress = register_reflection_event(seed_context, output_text)
        if ingress:
            event_id = ingress.get("event_id")
            runtime_context["event_id"] = event_id
            runtime_context.setdefault("trace_id", ingress.get("trace_id"))
            runtime_context.setdefault("decision_id", ingress.get("decision_id"))
            runtime_context.setdefault("position_id", ingress.get("position_id"))
            runtime_context.setdefault("l11_session_id", ingress.get("l11_session_id"))
            runtime_context.setdefault("current_stage", ingress.get("current_stage"))
            runtime_context.setdefault("current_stage_status", ingress.get("current_stage_status"))
            runtime_context.setdefault("event_type", ingress.get("event_type"))
            print(f"[L11] reflection ledger seeded | source={source} event_id={event_id}")
        else:
            print(f"[L11] reflection ledger seed skipped | source={source}")
    context = runtime_context

    async def _note(stage: str, status: str, payload: dict | None = None, error: str | None = None) -> None:
        if not event_id:
            return
        note_reflection_stage(
            event_id,
            stage=stage,
            source=source,
            status=status,
            payload=payload or {},
            error=error,
            completed_at=datetime.utcnow().isoformat() if status in {"completed", "skipped", "failed"} else None,
        )

    try:
        # Step 1: Critic (sync in thread to not block event loop)
        await _note("critic", "started", {
            "prompt_target": prompt_target,
            "prompt_version": prompt_version,
        })
        critique = await asyncio.to_thread(
            critique_output,
            output_text,
            source,
            effective_session_id,
            context,
            prompt_target,
            prompt_version,
        )
        await _note("critic", "completed", {
            "score": critique.get("score"),
            "verdict": critique.get("verdict"),
            "failure_category": critique.get("failure_category"),
        })

        # Step 2: Causal + Reflect in parallel
        async def _run_causal() -> dict:
            await _note("causal", "started")
            try:
                result = await extract_causality(output_text, source, effective_session_id, context)
                await _note("causal", "completed", {
                    "reasoning_type": result.get("reasoning_type"),
                    "confidence": result.get("confidence"),
                })
                return result
            except Exception as e:
                await _note("causal", "failed", error=str(e))
                raise

        async def _run_reflect() -> dict:
            if critique.get("verdict") == "ok":
                await _note("reflect", "skipped", {"reason": "verdict=ok"})
                return {"ok": True, "skipped": True, "reason": "verdict=ok"}
            await _note("reflect", "started")
            try:
                result = await reflect_on_gaps(output_text, critique, source, effective_session_id, prompt_target)
                if isinstance(result, dict) and result.get("ok"):
                    await _note("reflect", "completed", {
                        "gap": result.get("gap"),
                        "gap_domain": result.get("gap_domain"),
                        "evo_worthy": result.get("evo_worthy"),
                    })
                else:
                    await _note("reflect", "failed", result if isinstance(result, dict) else None)
                return result
            except Exception as e:
                await _note("reflect", "failed", error=str(e))
                raise

        causal_task = asyncio.create_task(_run_causal())
        reflect_task = asyncio.create_task(_run_reflect())
        causal_result, reflection = await asyncio.gather(
            causal_task, reflect_task, return_exceptions=True
        )

        # Step 3: Meta evaluator
        if isinstance(reflection, Exception):
            print(f"[L11] reflect failed: {reflection}")
            reflection = {}
        if not isinstance(reflection, dict):
            reflection = {}

        await _note("meta", "started")
        meta_result = await evaluate(critique, reflection, source, context=context)
        await _note("meta", "completed", {
            "action": meta_result.get("action"),
            "pattern": meta_result.get("pattern"),
        })
        if event_id:
            finalize_reflection_event(
                event_id,
                status="complete" if critique.get("verdict") != "fail" else "complete",
                current_stage="meta",
                current_stage_status="completed",
            )

        print(f"[L11] Pipeline complete | verdict={critique.get('verdict')} source={source}")

    except Exception as e:
        if event_id:
            finalize_reflection_event(
                event_id,
                status="failed",
                current_stage="meta",
                current_stage_status="failed",
                last_error=str(e),
            )
        print(f"[L11] Pipeline error (non-fatal): {e}")


# ── Convenience wrappers for each source type ─────────────────────────────────

def fire_session(output_text: str, session_id: str = "", context: dict = None) -> None:
    """Call from core_orch_layer10 after sending Telegram response."""
    asyncio.create_task(layer11_post_output(
        output_text=output_text,
        source="session",
        session_id=session_id,
        context=context,
    ))


def fire_autonomous(output_text: str, session_id: str = "", context: dict = None) -> None:
    """Call from autonomous task execution (L4 agent mode)."""
    asyncio.create_task(layer11_post_output(
        output_text=output_text,
        source="autonomous",
        session_id=session_id,
        context=context,
    ))


def fire_background_research(output_text: str) -> None:
    """Call from background_researcher() in core_train.py after each cycle output."""
    try:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(layer11_post_output(
                output_text=output_text,
                source="background_research",
            ))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(layer11_post_output(
                output_text=output_text,
                source="background_research",
            ))
            loop.close()
            asyncio.set_event_loop(None)
    except Exception as e:
        print(f"[L11] fire_background_research error: {e}")


def fire_system_prompt(prompt_text: str, target: str, version: int) -> None:
    """
    Call periodically to evaluate a system prompt version.
    E.g. after background_researcher completes N cycles on this prompt.
    """
    try:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(layer11_post_output(
                output_text=prompt_text,
                source="system_prompt",
                prompt_target=target,
                prompt_version=version,
            ))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(layer11_post_output(
                output_text=prompt_text,
                source="system_prompt",
                prompt_target=target,
                prompt_version=version,
            ))
            loop.close()
            asyncio.set_event_loop(None)
    except Exception as e:
        print(f"[L11] fire_system_prompt error: {e}")


def fire_trading(output_text: str, context: dict = None) -> None:
    """
    Call from core-trading-bot (via core_bridge.py) after every trade close.

    The trading bot writes hot_reflections + KB + mistakes directly to Supabase.
    This wrapper fires the full critic → causal → reflect → meta pipeline on top,
    enabling CORE to auto-generate behavioral_rules(domain=trading) evolutions.

    source='trading' routes through the standard critic with domain context.
    context should include: symbol, strategy, regime, bias, pnl, close_reason.

    Non-blocking — trading bot fires and forgets.
    """
    try:
        ctx = context or {}
        print(
            f"[L11] fire_trading trace_id={ctx.get('trace_id')} "
            f"decision_id={ctx.get('decision_id')} position_id={ctx.get('position_id')}"
        )
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(layer11_post_output(
                output_text=output_text,
                source="trading",
                context=ctx,
            ))
        else:
            loop.run_until_complete(layer11_post_output(
                output_text=output_text,
                source="trading",
                context=ctx,
            ))
    except Exception as e:
        print(f"[L11] fire_trading error: {e}")
