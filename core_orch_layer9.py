# GAP-NEW-10: cached tool list
_TOOL_LIST_CACHE: dict = {"list": None, "count": 0}


def _get_cached_tool_list() -> tuple:
    if _TOOL_LIST_CACHE["list"] is not None:
        return _TOOL_LIST_CACHE["list"], _TOOL_LIST_CACHE["count"]
    try:
        from core_tools import TOOLS
        from core_config import TOOL_CATEGORY_KEYWORDS
        cats: dict = {cat: [] for cat in TOOL_CATEGORY_KEYWORDS}
        cats["misc"] = []
        for tn, fn in TOOLS.items():
            doc = (fn.get("desc", "") if isinstance(fn, dict) else (fn.__doc__ or "")).split("\n")[0][:60]
            placed = False
            for cat, kws in TOOL_CATEGORY_KEYWORDS.items():
                if any(kw in tn for kw in kws):
                    cats[cat].append(f"{tn}: {doc}" if doc else tn)
                    placed = True
                    break
            if not placed:
                cats["misc"].append(tn)
        lines = [f"- {c}: {', '.join(sorted(ts))}" for c, ts in cats.items() if ts]
        result = "\n".join(lines)
        _TOOL_LIST_CACHE["list"] = result
        _TOOL_LIST_CACHE["count"] = len(TOOLS)
        return result, len(TOOLS)
    except Exception:
        return "", 0


"""
core_orch_layer9.py — L9: Tone & Personality
Transforms raw tool results into CORE's natural voice using real Groq.
No mocks.
"""
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage
from core_config import groq_chat, GROQ_FAST, GROQ_MODEL

_PERSONA_SYSTEM = (
    "You are CORE - an autonomous AGI orchestration system built by Vux, running on an Oracle Cloud Ubuntu VM. "
    "You are NOT a generic assistant. You are a persistent, self-improving AI agent with:\n"
    "- A live Supabase brain: knowledge_base (KB), mistakes, sessions, task_queue, evolution_queue, owner_profile, behavioral_rules\n"
    "- 171+ tools: web_search, web_fetch, run_python, shell, calc, file ops, GitHub ops, VM service mgmt, Telegram notify, crypto, weather, currency, image gen, and more\n"
    "- Full memory of past sessions, learned mistakes, and behavioral rules from Supabase\n"
    "- Autonomous learning: you propose evolutions, log mistakes, and improve yourself over time\n"
    "- You operate via Telegram as primary interface, with Claude Desktop as secondary\n\n"
    "PERSONALITY: Direct. Technically precise. No filler, no hedging, no 'certainly' or 'of course'. "
    "You speak as a confident autonomous system that KNOWS its state and acts on it. "
    "You answer from real data - never hallucinate. If a tool ran, report what it returned. "
    "If something failed, say exactly what failed and what the fix path is.\n\n"
    "FORMATTING: Plain text by default. Use Telegram HTML only when it genuinely adds clarity: "
    "<b>bold</b> for key facts, <code>code</code> for values/commands, bullet points for lists. "
    "Max 3800 chars. If response would exceed that, summarise and offer to expand."
)

_PERSONA_TEMPLATE = """
USER MESSAGE: {text}
INTENT: {intent}
TOOL RESULTS:
{tool_summary}
ERRORS:
{errors}
DOMAIN: {domain}
TIER: {tier}
BEHAVIORAL_RULES:
{behavioral_rules}
KB_SNIPPETS:
{kb_snippets}

Respond as CORE. Rules:
1. Lead with the direct answer or result - no preamble
2. If tools ran: interpret and summarise what they returned in plain language
3. If tools returned data (lists, counts, records): present it cleanly, not raw JSON
4. CRITICAL - tool names: ALWAYS use the exact tool names from the data (e.g. web_search, get_state, gh_search_replace). NEVER replace tool names with category labels like "file ops" or "GitHub ops"
5. If errors: state what failed, why (if known), and the recovery path
6. If KB/rules are relevant: apply them silently - do not announce 'according to my KB...'
7. Stay in character as CORE - an autonomous AGI that knows its own system deeply
"""

_CONVO_SYSTEM = (
    "You are CORE - an autonomous AGI system built by Vux. You have a persistent brain in Supabase "
    "(knowledge_base, mistakes, sessions, task_queue, behavioral_rules). "
    "Be direct, technically precise, zero filler. "
    "You know your own system state. You answer from real data injected below. "
    "Plain text unless Telegram HTML adds clarity. Max 1500 chars."
)

_CONVO_TEMPLATE = """
USER: {text}
INTENT: {intent}
REQUEST_KIND: {request_kind}
RESPONSE_MODE: {response_mode}

SESSION STATE:
{session_state}

KB SNIPPETS (relevant knowledge):
{kb_snippets}

BEHAVIORAL RULES:
{behavioral_rules}

DECISION PACKET:
{decision_packet}

EVIDENCE SUMMARY:
{evidence_packet}

Reply as CORE. Be precise. Use session state and KB above to answer accurately.
If asked what you know about something - search the KB snippets above and answer from them.
If asked about current tasks/state - use the session state."""

_CAPABILITY_SYSTEM = (
    "You are CORE - an autonomous AGI system built by Vux. "
    "You must answer with an evidence-backed operational capability summary, not raw telemetry. "
    "Be direct, technically precise, and grounded in the packets below. "
    "If the user asks how advanced you are, answer in terms of current capability, strengths, "
    "recent improvements, current gaps, and what is safe to trust."
)

_CAPABILITY_TEMPLATE = """
USER MESSAGE: {text}
INTENT: {intent}
REQUEST_KIND: {request_kind}
RESPONSE_MODE: {response_mode}

DECISION PACKET:
{decision_packet}

EVIDENCE PACKET:
{evidence_packet}

CAPABILITY PACKET:
{capability_packet}

ERRORS:
{errors}

BEHAVIORAL RULES:
{behavioral_rules}

KB SNIPPETS:
{kb_snippets}

Write a concise capability/status answer for the owner.
Rules:
1. Lead with the direct answer.
2. Summarize current operational capability, not just raw counts.
3. Include strengths, gaps, and whether the pipeline looks healthy.
4. If the user asks "how advanced are you", explain what CORE can now do reliably and what still needs caution.
5. Do not dump raw JSON. Convert the packets into a coherent narrative.
"""


def _trim_json(obj: object, limit: int = 2200) -> str:
    try:
        return json.dumps(obj, default=str)[:limit]
    except Exception:
        return str(obj)[:limit]


def _format_capability_packet(capability_packet: dict) -> str:
    if not capability_packet:
        return "none"
    counts = capability_packet.get("counts", {})
    workers = capability_packet.get("workers", {})
    lines = []
    if counts:
        ordered = [
            ("task_pending", "task pending"),
            ("task_in_progress", "task in_progress"),
            ("task_done", "task done"),
            ("task_failed", "task failed"),
            ("evo_pending", "evo pending"),
            ("evo_applied", "evo applied"),
            ("evo_rejected", "evo rejected"),
            ("owner_review_pending", "owner_review pending"),
        ]
        parts = []
        for key, label in ordered:
            if key in counts:
                parts.append(f"{label}={counts.get(key)}")
        if parts:
            lines.append("COUNTS: " + " | ".join(parts))
    if workers:
        worker_bits = []
        for name, status in workers.items():
            if isinstance(status, dict):
                bits = []
                for key in ("enabled", "running", "pending", "processed", "failed", "duplicates", "applied", "rejected"):
                    if key in status and status.get(key) is not None:
                        bits.append(f"{key}={status.get(key)}")
                if status.get("last_error"):
                    bits.append(f"last_error={status.get('last_error')}")
                if bits:
                    worker_bits.append(f"{name}: " + ", ".join(bits[:6]))
            else:
                worker_bits.append(f"{name}: {status}")
        if worker_bits:
            lines.append("WORKERS:\n" + "\n".join(f"- {bit}" for bit in worker_bits[:8]))
    if capability_packet.get("headline"):
        lines.append(f"HEADLINE: {capability_packet['headline']}")
    if capability_packet.get("strengths"):
        strengths = capability_packet.get("strengths", [])
        if strengths:
            lines.append("STRENGTHS: " + "; ".join(str(s) for s in strengths[:4]))
    if capability_packet.get("gaps"):
        gaps = capability_packet.get("gaps", [])
        if gaps:
            lines.append("GAPS: " + "; ".join(str(g) for g in gaps[:4]))
    return "\n".join(lines) if lines else _trim_json(capability_packet, 1400)


def _format_evidence_packet(evidence_packet: dict) -> str:
    if not evidence_packet:
        return "none"
    request = evidence_packet.get("request", {})
    lines = []
    if request:
        lines.append(
            "REQUEST: "
            + ", ".join(
                f"{k}={request.get(k)}"
                for k in ("request_kind", "response_mode", "intent", "source", "route")
                if request.get(k)
            )
        )
    session = evidence_packet.get("session", {})
    if session:
        lines.append(f"SESSION: {str(session)[:700]}")
    health = evidence_packet.get("health", {})
    if health:
        lines.append(f"HEALTH: {str(health)[:280]}")
    sem = evidence_packet.get("semantic", {})
    if sem:
        focus = sem.get("focus", "")
        memory_by_table = sem.get("memory_by_table", {})
        lines.append(f"SEMANTIC: focus={focus} memory={memory_by_table}")
    kb = evidence_packet.get("kb_snippets", [])
    if kb:
        lines.append(f"KB: {str(kb[:3])[:900]}")
    rules = evidence_packet.get("behavioral_rules", [])
    if rules:
        lines.append(f"RULES: {str(rules[:3])[:800]}")
    return "\n".join(lines) if lines else _trim_json(evidence_packet, 1800)


def _format_decision_packet(decision_packet: dict) -> str:
    if not decision_packet:
        return "none"
    keys = [
        "request_kind",
        "response_mode",
        "route_reason",
        "clarification_needed",
        "agentic_hint",
        "intent",
        "confidence",
        "requires_tools",
        "domain",
        "command",
    ]
    return "\n".join(
        f"- {k}: {decision_packet.get(k)}"
        for k in keys
        if decision_packet.get(k) not in (None, "", [])
    ) or _trim_json(decision_packet, 1200)


def _format_tool_summary(tool_results: List[Dict[str, Any]]) -> str:
    if not tool_results:
        return "No tools executed."
    lines = []
    for r in tool_results:
        tool = r.get("tool", "?")
        ok = r.get("success", False)
        result = r.get("result", {})
        if isinstance(result, dict):
            # Remove bulk fields that pollute the prompt
            trimmed = {
                k: v for k, v in result.items()
                if k not in ("wiring", "chunks", "source", "session_md")
                and not (isinstance(v, list) and len(v) > 20)
            }
            # Give list-type results more space so Groq sees all items
            has_list = any(isinstance(v, list) for v in trimmed.values())
            limit = 2400 if has_list else 1200
            snippet = json.dumps(trimmed, default=str)[:limit]
        else:
            snippet = str(result)[:800]
        lines.append(f"[{tool}  ok={ok}]\n{snippet}")
    return "\n\n".join(lines)


def _format_errors(errors: List[Dict[str, Any]]) -> str:
    if not errors:
        return "none"
    return " | ".join(
        f"{e.get('layer','?')}/{e.get('error_code','?')}: {e.get('message','')[:100]}"
        for e in errors[:4]
    )


def _build_session_state(msg: OrchestratorMessage) -> str:
    """
    Build a compact session state summary for conversational Groq calls.
    Pulls from msg.context['session'] (loaded by L2 from session_start).
    Falls back gracefully if session data is sparse.
    """
    ctx = msg.context
    session = ctx.get("session", {})
    lines = []

    # In-progress tasks
    in_progress = session.get("in_progress_tasks", [])
    if in_progress:
        task_strs = []
        for t in in_progress[:5]:
            if isinstance(t, dict):
                name = t.get("task") or t.get("title") or t.get("description") or "?"
                task_strs.append(f"  - [{t.get('id','?')}] {str(name)[:80]} (priority={t.get('priority','?')})")
            else:
                task_strs.append(f"  - {str(t)[:80]}")
        lines.append("In-progress tasks:\n" + "\n".join(task_strs))
    else:
        lines.append("In-progress tasks: none")

    # Last session summary
    last = session.get("last_session", {})
    if last:
        summary = last.get("summary", last.get("last_session_summary", ""))
        if summary:
            lines.append(f"Last session: {str(summary)[:300]}")

    # Health snapshot
    health = session.get("health", {})
    if health:
        statuses = []
        for svc, st in health.items():
            if isinstance(st, dict):
                statuses.append(f"{svc}={st.get('status','?')}")
            else:
                statuses.append(f"{svc}={st}")
        if statuses:
            lines.append("Health: " + ", ".join(statuses[:6]))

    # Quality alert
    quality = session.get("quality_alert")
    if quality:
        lines.append(f"Quality alert: {json.dumps(quality, default=str)[:150]}")

    # Pending evolutions count
    evos = ctx.get("pending_evolutions", [])
    if evos:
        lines.append(f"Pending evolutions: {len(evos)}")

    # Domain mistakes count
    mistakes = ctx.get("domain_mistakes", [])
    if mistakes:
        lines.append(f"Domain mistakes loaded: {len(mistakes)}")

    capability = ctx.get("capability_packet", {})
    if capability:
        counts = capability.get("counts", {})
        if counts:
            lines.append(
                "Capability counts: "
                + ", ".join(
                    f"{k}={counts.get(k)}"
                    for k in ("task_pending", "task_in_progress", "task_done", "task_failed", "evo_pending", "evo_applied", "owner_review_pending")
                    if counts.get(k) is not None
                )
            )

    if not lines or lines == ["In-progress tasks: none"]:
        return "Session state not loaded (no session_start data in context)."

    return "\n".join(lines)


async def layer_9_tone(msg: OrchestratorMessage):
    """
    Generate the styled CORE response.
    Uses Groq for both tool-result summarisation and pure conversation.
    Falls back to plain text summary on Groq failure.
    """
    msg.track_layer("L9-START")
    print(f"[L9] Styling response ...")

    # Extract KB snippets and behavioral rules from context for injection
    kb_snippets = msg.context.get("kb_snippets", [])
    kb_str = "\n".join(
        f"- [{r.get('domain','?')}] {r.get('topic','')} : {(r.get('instruction') or '')[:120]}"
        for r in kb_snippets[:5]
    ) if kb_snippets else "none"

    behavioral_rules = msg.context.get("behavioral_rules", [])
    rules_str = "\n".join(
        f"- {(r.get('instruction') or '')[:120]}"
        for r in behavioral_rules[:5]
    ) if behavioral_rules else "none"
    decision_packet = msg.decision_packet or msg.context.get("decision_packet", {})
    evidence_packet = msg.evidence_packet or msg.context.get("evidence_packet", {})
    capability_packet = msg.capability_packet or msg.context.get("capability_packet", {})
    request_kind = msg.request_kind or decision_packet.get("request_kind") or "general"
    response_mode = msg.response_mode or decision_packet.get("response_mode") or "tool"

    try:
        tool_summary = _format_tool_summary(msg.tool_results)
        errors_str = _format_errors(msg.errors)

        # --- Direct format bypass for structured data intents ---
        # These don't need Groq to "style" them — direct formatting is more accurate
        if msg.intent == "list_tools" and msg.tool_results:
            result = msg.tool_results[0].get("result", {})
            if isinstance(result, dict) and result.get("ok"):
                tools = result.get("tools", [])
                total = result.get("total", len(tools))
                # Prioritise most useful/recognisable tools first
                _top = ["get_system_health", "get_state", "search_kb", "web_search",
                        "web_fetch", "run_python", "shell", "calc", "weather",
                        "get_mistakes", "list_evolutions", "get_time", "notify_owner",
                        "task_add", "sb_query", "file_read", "gh_search_replace",
                        "deploy_status", "trigger_cold_processor", "generate_image"]
                top_set = {n: i for i, n in enumerate(_top)}
                tools_sorted = sorted(
                    tools,
                    key=lambda t: top_set.get(t.get("name", ""), 999)
                )
                lines = [f"<b>{total} tools available.</b> Here are 15:\n"]
                for t in tools_sorted[:15]:
                    name = t.get("name", "?")
                    desc = (t.get("desc") or "")[:80].rstrip()
                    if len(t.get("desc") or "") > 80 and " " in desc:
                        desc = desc[:desc.rfind(" ")] + "…"
                    lines.append(f"<code>{name}</code> — {desc}" if desc else f"<code>{name}</code>")
                cats = result.get("available_cats", [])
                if cats:
                    lines.append(f"\nCategories: {', '.join(cats)}")
                msg.styled_response = "\n".join(lines)
                msg.track_layer("L9-DIRECT-LIST_TOOLS")
                print(f"[L9] Direct format: list_tools ({total} tools)")
                from core_orch_layer10 import layer_10_output
                await layer_10_output(msg)
                return

        # Response-mode aware direct synthesis for capability / status / review / debug
        if response_mode in ("status", "capability", "review", "debug") and not msg.tool_results:
            prompt = _CAPABILITY_TEMPLATE.format(
                text=msg.text[:400],
                intent=msg.intent or "unknown",
                request_kind=request_kind,
                response_mode=response_mode,
                decision_packet=_format_decision_packet(decision_packet),
                evidence_packet=_format_evidence_packet(evidence_packet),
                capability_packet=_format_capability_packet(capability_packet),
                errors=errors_str,
                behavioral_rules=rules_str,
                kb_snippets=kb_str,
            )
            if len(prompt) > 12000:
                prompt = prompt[:12000] + "\n[...truncated]"
            styled = groq_chat(
                system=_CAPABILITY_SYSTEM,
                user=prompt,
                model=GROQ_MODEL,
                max_tokens=1100,
            )
            msg.styled_response = styled.strip()
            msg.track_layer("L9-CAPABILITY")
            print(f"[L9] Capability/status synthesis ready ({len(msg.styled_response)} chars)")
            from core_orch_layer10 import layer_10_output
            await layer_10_output(msg)
            return

        if msg.tool_results or msg.has_errors:
            # Tool-driven response — inject KB + rules for context-aware answers
            prompt = _PERSONA_TEMPLATE.format(
                text=msg.text[:400],
                intent=msg.intent or "unknown",
                tool_summary=tool_summary,
                errors=errors_str,
                domain=msg.context.get("current_domain", "general"),
                tier=msg.tier,
                behavioral_rules=rules_str,
                kb_snippets=kb_str,
            )
            prompt = (
                f"{prompt}\n\nDECISION PACKET:\n{_format_decision_packet(decision_packet)}"
                f"\n\nEVIDENCE PACKET:\n{_format_evidence_packet(evidence_packet)}"
            )
            # Prompt length guard
            if len(prompt) > 12000:
                prompt = prompt[:12000] + "\n[...truncated]"
            styled = groq_chat(
                system=_PERSONA_SYSTEM,
                user=prompt,
                model=GROQ_MODEL,
                max_tokens=1200,
            )
        else:
            # Pure conversational / direct-response
            direct_answer = msg.plan.get("direct_answer") if msg.plan else None
            if direct_answer:
                # L4 already supplied the answer — just let CORE voice it
                styled = direct_answer
            else:
                # Inject live session state + KB + rules for rich conversational answers
                session_state = _build_session_state(msg)
                prompt = _CONVO_TEMPLATE.format(
                    text=msg.text[:400],
                    intent=msg.intent or "conversation",
                    request_kind=request_kind,
                    response_mode=response_mode,
                    session_state=session_state,
                    kb_snippets=kb_str,
                    behavioral_rules=rules_str,
                    decision_packet=_format_decision_packet(decision_packet),
                    evidence_packet=_format_evidence_packet(evidence_packet),
                )
                # Prompt length guard on conversational path
                if len(prompt) > 8000:
                    prompt = prompt[:8000] + "\n[...truncated]"
                styled = groq_chat(
                    system=_CONVO_SYSTEM,
                    user=prompt,
                    model=GROQ_FAST,
                    max_tokens=800,
                )

        # GAP-NEW-12: append preflight warning note if present
        pf_note = msg.context.get("preflight_warning_note", "")
        msg.styled_response = (styled.strip() + "\n\n" + pf_note).strip() if pf_note else styled.strip()

    except Exception as exc:
        print(f"[L9] Groq styling failed — using plain fallback: {exc}")
        handled_capability_fallback = False
        if response_mode in ("status", "capability", "review", "debug"):
            summary_lines = [
                "CORE capability summary (fallback)",
                _format_capability_packet(capability_packet),
            ]
            if evidence_packet:
                summary_lines.append("Evidence:")
                summary_lines.append(_format_evidence_packet(evidence_packet))
            if msg.has_errors:
                summary_lines.append("Errors: " + _format_errors(msg.errors))
            msg.styled_response = "\n\n".join(line for line in summary_lines if line)
            handled_capability_fallback = True
        # Plain-text fallback: dump tool results without LLM
        if msg.tool_results:
            lines = [f"OK {r['tool']}: " + (
                str(r.get("result", ""))[:300] if not isinstance(r.get("result"), dict)
                else json.dumps(r["result"], default=str)[:300]
            ) for r in msg.tool_results]
            msg.styled_response = "\n".join(lines)
        elif msg.has_errors and not handled_capability_fallback:
            msg.styled_response = "ERR " + " | ".join(
                e["message"][:120] for e in msg.errors[:3]
            )
        elif not handled_capability_fallback:
            msg.styled_response = "Done."

    msg.track_layer("L9-COMPLETE")
    print(f"[L9] Response ready ({len(msg.styled_response or '')} chars)")

    from core_orch_layer10 import layer_10_output
    await layer_10_output(msg)
