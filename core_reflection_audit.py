"""core_reflection_audit.py -- durable reflection event ledger helpers.

This module centralizes the branch-agnostic audit trail used by CORE's L11
pipeline. It writes a canonical event row plus per-stage progress rows so
trading today and future branches later can share the same backbone.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from core_config import SUPABASE_PAT, SUPABASE_REF, SUPABASE_URL, sb_get, sb_patch, sb_post, sb_upsert

REFLECTION_EVENTS_TABLE = "reflection_events"
REFLECTION_EVENT_STAGES_TABLE = "reflection_event_stages"
DEFAULT_SOURCE_SERVICE = "core-agi"
DEFAULT_SOURCE_DOMAIN = "core"

REFLECTION_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS reflection_events (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID UNIQUE NOT NULL,
    source TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    source_branch TEXT NOT NULL,
    source_service TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'reflection',
    trace_id TEXT NOT NULL,
    decision_id TEXT,
    position_id TEXT,
    session_id UUID,
    symbol TEXT,
    strategy TEXT,
    strategy_family TEXT,
    regime_at_entry TEXT,
    bias_at_entry TEXT,
    verdict TEXT,
    pnl_usd DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION,
    funding_usd DOUBLE PRECISION,
    capital_usd DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    close_reason TEXT,
    output_text TEXT NOT NULL,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    producer_created_at TIMESTAMPTZ,
    core_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'received',
    current_stage TEXT NOT NULL DEFAULT 'ingress',
    current_stage_status TEXT NOT NULL DEFAULT 'received',
    l11_session_id UUID,
    critic_at TIMESTAMPTZ,
    causal_at TIMESTAMPTZ,
    reflect_at TIMESTAMPTZ,
    meta_at TIMESTAMPTZ,
    last_error TEXT,
    artifacts JSONB NOT NULL DEFAULT '{}'::jsonb,
    prompt_target TEXT,
    prompt_version INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS reflection_events_event_id_uidx
    ON reflection_events (event_id);
CREATE INDEX IF NOT EXISTS reflection_events_trace_id_idx
    ON reflection_events (trace_id);
CREATE INDEX IF NOT EXISTS reflection_events_decision_id_idx
    ON reflection_events (decision_id);
CREATE INDEX IF NOT EXISTS reflection_events_position_id_idx
    ON reflection_events (position_id);
CREATE INDEX IF NOT EXISTS reflection_events_source_idx
    ON reflection_events (source_domain, source_branch, source_service);

CREATE TABLE IF NOT EXISTS reflection_event_stages (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES reflection_events(event_id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS reflection_event_stages_event_stage_uidx
    ON reflection_event_stages (event_id, stage_name);
CREATE INDEX IF NOT EXISTS reflection_event_stages_event_idx
    ON reflection_event_stages (event_id);
CREATE INDEX IF NOT EXISTS reflection_event_stages_stage_idx
    ON reflection_event_stages (stage_name);

CREATE OR REPLACE VIEW trading_reflection_events AS
SELECT *
FROM reflection_events
WHERE source_domain = 'trading';
"""


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)


def _maybe_uuid(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        return ""


def reflection_audit_ddl() -> str:
    """Return the canonical SQL used to create the reflection audit ledger."""
    return REFLECTION_AUDIT_DDL.strip()





def _is_transient_supabase_error(text: str) -> bool:
    lowered = (text or '').lower()
    return any(token in lowered for token in (
        'recovery mode',
        'not accepting connections',
        'hot standby mode is disabled',
        'econnreset',
        'client network socket disconnected',
        'could not connect',
        'timed out',
    ))


def apply_reflection_audit_schema() -> bool:
    """Apply the reflection audit DDL through the Supabase management API."""
    if not SUPABASE_PAT:
        return False
    try:
        stmts = [stmt.strip() for stmt in reflection_audit_ddl().split(";") if stmt.strip()]
        ok = True
        for stmt in stmts:
            attempts = 0
            while True:
                attempts += 1
                resp = httpx.post(
                    f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
                    headers={
                        "Authorization": f"Bearer {SUPABASE_PAT}",
                        "Content-Type": "application/json",
                    },
                    json={"query": stmt + ";"},
                    timeout=45,
                )
                if resp.status_code in (200, 201):
                    break
                text = resp.text[:300]
                if attempts < 12 and _is_transient_supabase_error(text):
                    wait = min(30, 2 ** attempts)
                    print(f"[REFLECTION_AUDIT] transient error, retrying in {wait}s: {resp.status_code} {text}")
                    time.sleep(wait)
                    continue
                print(f"[REFLECTION_AUDIT] DDL failed: {resp.status_code} {text}")
                ok = False
                break
            if not ok:
                break
        return ok
    except Exception as e:
        print(f"[REFLECTION_AUDIT] DDL error: {e}")
        return False


def _stable_event_seed(source: str, context: dict | None, output_text: str) -> str:
    ctx = context or {}
    parts = {
        "source": source or "unknown",
        "source_domain": _text(ctx.get("source_domain") or ctx.get("domain") or DEFAULT_SOURCE_DOMAIN),
        "source_branch": _text(ctx.get("source_branch") or ctx.get("branch") or source or DEFAULT_SOURCE_DOMAIN),
        "source_service": _text(ctx.get("source_service") or DEFAULT_SOURCE_SERVICE),
        "trace_id": _text(ctx.get("trace_id")),
        "decision_id": _text(ctx.get("decision_id")),
        "position_id": _text(ctx.get("position_id")),
        "session_id": _text(ctx.get("session_id")),
        "symbol": _text(ctx.get("symbol")),
        "strategy": _text(ctx.get("strategy")),
        "close_reason": _text(ctx.get("close_reason")),
        "output_hash": hashlib.sha256((output_text or "").strip().encode()).hexdigest()[:24],
    }
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))


def derive_event_id(source: str, context: dict | None, output_text: str) -> str:
    """Return a stable audit id.

    If the producer already supplied event_id/audit_id, preserve it. Otherwise
    derive a deterministic UUID5 from the event fingerprint.
    """
    ctx = context or {}
    for key in ("event_id", "audit_id"):
        candidate = _maybe_uuid(ctx.get(key))
        if candidate:
            return candidate
        raw = _text(ctx.get(key))
        if raw:
            # Allow a non-UUID upstream id while still normalizing the schema.
            return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))

    seed = _stable_event_seed(source, context, output_text)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def normalize_reflection_event(
    output_text: str,
    source: str = "session",
    context: dict | None = None,
    session_id: str = "",
    prompt_target: str = "",
    prompt_version: int = 0,
    source_service: str = DEFAULT_SOURCE_SERVICE,
    source_domain: str = DEFAULT_SOURCE_DOMAIN,
    source_branch: str = "",
) -> dict:
    """Build the canonical reflection event payload."""
    ctx = dict(context or {})
    if source_branch:
        ctx.setdefault("source_branch", source_branch)
    if source_service:
        ctx.setdefault("source_service", source_service)
    if source_domain:
        ctx.setdefault("source_domain", source_domain)

    event_id = derive_event_id(source, ctx, output_text)
    trace_id = _text(ctx.get("trace_id"))
    decision_id = _text(ctx.get("decision_id"))
    position_id = _text(ctx.get("position_id"))
    session_uuid = _maybe_uuid(session_id or ctx.get("session_id")) or None
    l11_session_id = _maybe_uuid(ctx.get("l11_session_id") or session_uuid) or None

    payload = {
        "event_id": event_id,
        "source": source or "unknown",
        "source_domain": _text(ctx.get("source_domain") or source_domain or DEFAULT_SOURCE_DOMAIN),
        "source_branch": _text(ctx.get("source_branch") or source_branch or source or DEFAULT_SOURCE_DOMAIN),
        "source_service": _text(ctx.get("source_service") or source_service or DEFAULT_SOURCE_SERVICE),
        "event_type": _text(ctx.get("event_type") or "reflection"),
        "trace_id": trace_id,
        "decision_id": decision_id,
        "position_id": position_id,
        "session_id": session_uuid,
        "symbol": _text(ctx.get("symbol")),
        "strategy": _text(ctx.get("strategy")),
        "strategy_family": _text(ctx.get("strategy_family")),
        "regime_at_entry": _text(ctx.get("regime_at_entry")),
        "bias_at_entry": _text(ctx.get("bias_at_entry")),
        "verdict": _text(ctx.get("verdict")),
        "pnl_usd": ctx.get("pnl"),
        "pnl_pct": ctx.get("pnl_pct"),
        "funding_usd": ctx.get("funding_usd", ctx.get("funding")),
        "capital_usd": ctx.get("capital"),
        "confidence": ctx.get("confidence"),
        "close_reason": _text(ctx.get("close_reason")),
        "output_text": (output_text or "").strip()[:4000],
        "context": ctx,
        "producer_created_at": ctx.get("producer_created_at") or ctx.get("closed_at") or _utcnow(),
        "core_received_at": _utcnow(),
        "status": _text(ctx.get("status") or "received"),
        "current_stage": _text(ctx.get("current_stage") or "ingress"),
        "current_stage_status": _text(ctx.get("current_stage_status") or "received"),
        "l11_session_id": l11_session_id,
        "critic_at": ctx.get("critic_at"),
        "causal_at": ctx.get("causal_at"),
        "reflect_at": ctx.get("reflect_at"),
        "meta_at": ctx.get("meta_at"),
        "last_error": _text(ctx.get("last_error")),
        "artifacts": ctx.get("artifacts") or {},
        "prompt_target": _text(prompt_target),
        "prompt_version": prompt_version,
        "updated_at": _utcnow(),
    }
    return payload


def build_reflection_context(
    context: dict | None,
    source_domain: str = DEFAULT_SOURCE_DOMAIN,
    source_branch: str = "",
    source_service: str = DEFAULT_SOURCE_SERVICE,
    output_text: str = "",
    source: str = "trading",
) -> dict:
    """Compatibility wrapper for callers that want a canonical event context."""
    return normalize_reflection_event(
        output_text=output_text,
        source=source,
        context=context,
        source_domain=source_domain,
        source_branch=source_branch,
        source_service=source_service,
    )


def ingest_reflection_event(payload: dict) -> bool:
    """Insert or update the canonical event row and seed the ingress stage."""
    if not payload or not payload.get("event_id"):
        return False

    row = dict(payload)
    # Keep context/artifacts JSON-native and trim any accidental None fields.
    row["context"] = row.get("context") or {}
    row["artifacts"] = row.get("artifacts") or {}
    ok = sb_upsert(REFLECTION_EVENTS_TABLE, row, on_conflict="event_id")
    if ok:
        record_reflection_stage(
            payload["event_id"],
            stage="ingress",
            status="received",
            source=row.get("source", "unknown"),
            details={
                "source_domain": row.get("source_domain"),
                "source_branch": row.get("source_branch"),
                "source_service": row.get("source_service"),
                "trace_id": row.get("trace_id"),
                "decision_id": row.get("decision_id"),
                "position_id": row.get("position_id"),
            },
        )
    return ok


def register_reflection_event(context: dict, output_text: str = "") -> dict | None:
    """Compatibility wrapper that persists and returns the canonical event payload."""
    payload = dict(context or {})
    if output_text and not payload.get("output_text"):
        payload["output_text"] = output_text
    if not payload.get("event_id"):
        payload = normalize_reflection_event(
            output_text=output_text,
            source=_text(payload.get("source") or "trading"),
            context=payload,
            source_domain=_text(payload.get("source_domain") or DEFAULT_SOURCE_DOMAIN),
            source_branch=_text(payload.get("source_branch") or "unknown"),
            source_service=_text(payload.get("source_service") or DEFAULT_SOURCE_SERVICE),
        )
    if not ingest_reflection_event(payload):
        return None
    return payload


def record_reflection_stage(
    event_id: str,
    stage: str,
    status: str = "done",
    source: str = "unknown",
    details: dict | None = None,
    payload: dict | None = None,
    error: str = "",
    completed_at: str | None = None,
) -> bool:
    """Upsert a stage progress row and mirror timestamps back to the event row."""
    if not event_id or not stage:
        return False

    now = _utcnow()
    stage_payload = payload if payload is not None else (details or {})
    stage_row = {
        "event_id": event_id,
        "stage_name": stage,
        "status": status,
        "source": source or "unknown",
        "details": stage_payload,
        "error": _text(error)[:1000] or None,
        "started_at": now,
        "completed_at": completed_at or (now if status in {"done", "complete", "completed", "skipped", "error", "failed"} else None),
        "updated_at": now,
    }
    ok = True
    try:
        existing = sb_get(
            REFLECTION_EVENT_STAGES_TABLE,
            f"select=id&event_id=eq.{event_id}&stage_name=eq.{stage}&limit=1",
            svc=True,
        ) or []
        if existing and existing[0].get("id"):
            ok = sb_patch(
                REFLECTION_EVENT_STAGES_TABLE,
                f"id=eq.{existing[0].get('id')}",
                stage_row,
            )
        else:
            ok = sb_post(REFLECTION_EVENT_STAGES_TABLE, stage_row)
    except Exception:
        ok = False

    event_patch = {"updated_at": now}
    if stage == "ingress":
        event_patch["status"] = "received" if status not in {"error", "failed"} else "error"
        event_patch["current_stage"] = "ingress"
        event_patch["current_stage_status"] = status
    elif stage == "critic":
        event_patch["critic_at"] = now
        event_patch["current_stage"] = "critic"
        event_patch["current_stage_status"] = status
    elif stage == "causal":
        event_patch["causal_at"] = now
        event_patch["current_stage"] = "causal"
        event_patch["current_stage_status"] = status
    elif stage == "reflect":
        event_patch["reflect_at"] = now
        event_patch["current_stage"] = "reflect"
        event_patch["current_stage_status"] = status
    elif stage == "meta":
        event_patch["meta_at"] = now
        event_patch["status"] = "complete" if status not in {"error", "failed"} else "error"
        event_patch["current_stage"] = "meta"
        event_patch["current_stage_status"] = status

    if error:
        event_patch["last_error"] = _text(error)[:1000]
        event_patch["status"] = "error"

    try:
        sb_patch(REFLECTION_EVENTS_TABLE, f"event_id=eq.{event_id}", event_patch)
    except Exception:
        pass

    return ok


def note_reflection_stage(
    event_id: str,
    stage: str,
    source: str = "unknown",
    status: str = "done",
    payload: dict | None = None,
    error: str = "",
    completed_at: str | None = None,
) -> bool:
    """Compatibility wrapper used by the L11 pipeline."""
    return record_reflection_stage(
        event_id=event_id,
        stage=stage,
        status=status,
        source=source,
        payload=payload,
        error=error,
        completed_at=completed_at,
    )


def finalize_reflection_event(
    event_id: str,
    status: str = "complete",
    error: str = "",
    current_stage: str = "",
    current_stage_status: str = "",
    last_error: str = "",
) -> bool:
    if not event_id:
        return False
    patch = {
        "status": status,
        "updated_at": _utcnow(),
    }
    if current_stage:
        patch["current_stage"] = current_stage
    if current_stage_status:
        patch["current_stage_status"] = current_stage_status
    if error or last_error:
        patch["last_error"] = _text(error or last_error)[:1000]
    return sb_patch(REFLECTION_EVENTS_TABLE, f"event_id=eq.{event_id}", patch)


def fetch_reflection_events(**kwargs) -> dict:
    """Compatibility wrapper used by the HTTP query handlers."""
    return query_reflection_events(**kwargs).get("events", [])


def query_reflection_events(
    event_id: str = "",
    trace_id: str = "",
    decision_id: str = "",
    position_id: str = "",
    source_domain: str = "",
    source_branch: str = "",
    source_service: str = "",
    limit: int = 25,
) -> dict:
    """Fetch canonical event rows with attached stage progress."""
    filters = []
    if event_id:
        filters.append(f"event_id=eq.{event_id}")
    if trace_id:
        filters.append(f"trace_id=eq.{trace_id}")
    if decision_id:
        filters.append(f"decision_id=eq.{decision_id}")
    if position_id:
        filters.append(f"position_id=eq.{position_id}")
    if source_domain:
        filters.append(f"source_domain=eq.{source_domain}")
    if source_branch:
        filters.append(f"source_branch=eq.{source_branch}")
    if source_service:
        filters.append(f"source_service=eq.{source_service}")

    qs = "select=*&order=core_received_at.desc"
    if filters:
        qs = "&".join([qs] + filters)
    qs += f"&limit={max(1, min(int(limit or 25), 200))}"
    events = sb_get(REFLECTION_EVENTS_TABLE, qs, svc=True)

    stage_map: dict[str, list[dict]] = {}
    if events:
        ids = [row.get("event_id") for row in events if row.get("event_id")]
        if ids:
            id_filters = ",".join(ids)
            stages = sb_get(
                REFLECTION_EVENT_STAGES_TABLE,
                f"select=*&event_id=in.({id_filters})&order=started_at.asc",
                svc=True,
            )
            for row in stages:
                stage_map.setdefault(row.get("event_id"), []).append(row)

    for row in events:
        row["stages"] = stage_map.get(row.get("event_id"), [])

    return {
        "ok": True,
        "count": len(events),
        "events": events,
        "filters": {
            "event_id": event_id or None,
            "trace_id": trace_id or None,
            "decision_id": decision_id or None,
            "position_id": position_id or None,
            "source_domain": source_domain or None,
            "source_branch": source_branch or None,
            "source_service": source_service or None,
        },
    }
