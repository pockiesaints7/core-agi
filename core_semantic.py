"""
core_semantic.py — CORE Unified Semantic Search Layer
======================================================
Single entry point for ALL meaning-based searches across CORE's tables.
Replaces ilike in every search function. ilike is REMOVED, not fallback.

Only exception: Voyage AI outage → emergency ilike fallback (logged, not silent).

Tables covered:
  knowledge_base    — primary KB search (L2 + t_search_kb)
  mistakes          — t_search_mistakes + predict_failure + log_mistake dedup
  behavioral_rules  — t_get_behavioral_rules semantic load
  pattern_frequency — dedup before cold processor
  hot_reflections   — cold processor semantic cluster
  output_reflections — L11 meta evaluator dedup
  evolution_queue   — evo dedup before inserting

Auto-embed on insert: call embed_on_insert(table, row_id, text) after every sb_post.
Backfill: call backfill_table(table) to embed all existing rows.
"""
import hashlib
import time
import asyncio
from datetime import datetime
from typing import Optional

import httpx

from core_config import SUPABASE_URL, SUPABASE_REF, SUPABASE_PAT, _sbh, sb_get, sb_post, sb_patch

# ── Constants ──────────────────────────────────────────────────────────────────
_EMBED_MODEL          = "voyage-3"
_EMBED_MODEL_FALLBACK = "voyage-3-lite"
_EMBED_DIM            = 1024
_DEFAULT_THRESHOLD    = 0.72
_MGMT_URL             = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"
_MGMT_HEADERS         = {"Authorization": f"Bearer {SUPABASE_PAT}", "Content-Type": "application/json"}

import os as _os
_VOYAGE_KEY = _os.environ.get("VOYAGE_API_KEY", "")

# ══════════════════════════════════════════════════════════════════════════════
# TABLE REGISTRY — text extractor + RPC name per table
# ══════════════════════════════════════════════════════════════════════════════

def _kb_text(r):
    return " | ".join(p for p in [r.get("topic",""), r.get("instruction",""), r.get("content","")] if p)

def _mistake_text(r):
    return " | ".join(p for p in [r.get("what_failed",""), r.get("context",""), r.get("root_cause",""), r.get("how_to_avoid","")] if p)

def _rule_text(r):
    return r.get("full_rule","") or r.get("trigger","") or ""

def _pattern_text(r):
    return r.get("pattern_key","") or r.get("description","") or ""

def _hot_text(r):
    parts = [r.get("reflection_text",""), r.get("task_summary","")]
    gaps = r.get("gaps_identified") or []
    if isinstance(gaps, list):
        parts.extend(gaps)
    return " | ".join(p for p in parts if p)

def _reflect_text(r):
    return " | ".join(p for p in [r.get("gap",""), r.get("new_behavior",""), r.get("gap_domain","")] if p)

def _evo_text(r):
    return " | ".join(p for p in [r.get("change_summary",""), r.get("pattern_key","")] if p)

SEMANTIC_TABLES = {
    "knowledge_base": {
        "text_fn":   _kb_text,
        "rpc":       "match_knowledge_base",
        "select":    "id,domain,topic,instruction,content,confidence",
        "threshold": 0.72,
    },
    "mistakes": {
        "text_fn":   _mistake_text,
        "rpc":       "match_mistakes",
        "select":    "id,domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity",
        "threshold": 0.75,
    },
    "behavioral_rules": {
        "text_fn":   _rule_text,
        "rpc":       "match_behavioral_rules",
        "select":    "id,trigger,pointer,full_rule,domain,priority,confidence",
        "threshold": 0.78,
    },
    "pattern_frequency": {
        "text_fn":   _pattern_text,
        "rpc":       "match_patterns",
        "select":    "id,pattern_key,frequency,domain,description",
        "threshold": 0.88,
    },
    "hot_reflections": {
        "text_fn":   _hot_text,
        "rpc":       "match_hot_reflections",
        "select":    "id,domain,task_summary,reflection_text,gaps_identified,quality_score",
        "threshold": 0.72,
    },
    "output_reflections": {
        "text_fn":   _reflect_text,
        "rpc":       "match_output_reflections",
        "select":    "id,source,gap,gap_domain,new_behavior,verdict",
        "threshold": 0.85,
    },
    "evolution_queue": {
        "text_fn":   _evo_text,
        "rpc":       "match_evolutions",
        "select":    "id,change_type,change_summary,pattern_key,status,confidence",
        "threshold": 0.85,
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDING
# ══════════════════════════════════════════════════════════════════════════════

def _get_embedding(text: str) -> list:
    """Voyage AI embedding. Primary: voyage-3, fallback: voyage-3-lite. Raises on both fail."""
    text = text.strip()[:8000]
    key = _os.environ.get("VOYAGE_API_KEY", "") or _VOYAGE_KEY
    if not key:
        raise RuntimeError("VOYAGE_API_KEY not set")
    for model in (_EMBED_MODEL, _EMBED_MODEL_FALLBACK):
        try:
            r = httpx.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "input": [text]},
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(10)
                r = httpx.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "input": [text]},
                    timeout=15,
                )
            r.raise_for_status()
            vec = r.json()["data"][0]["embedding"]
            if vec:
                return vec
        except Exception as e:
            print(f"[SEMANTIC] embed {model} failed: {e}")
    raise RuntimeError("All Voyage embedding attempts failed")

def _embed_safe(text: str) -> list:
    """Non-raising embed. Returns [] on failure."""
    try:
        return _get_embedding(text)
    except Exception as e:
        print(f"[SEMANTIC] _embed_safe failed: {e}")
        return []

# ══════════════════════════════════════════════════════════════════════════════
# CORE SEARCH — single entry point
# ══════════════════════════════════════════════════════════════════════════════

def search(
    table: str,
    query: str,
    limit: int = 10,
    threshold: float = None,
    filters: str = "",
) -> list:
    """
    Semantic search against any registered table.
    Replaces ALL ilike searches in CORE.
    Emergency ilike fallback ONLY if Voyage AI is completely down.

    Args:
        table:     One of SEMANTIC_TABLES keys
        query:     Natural language search string
        limit:     Max results
        threshold: Cosine similarity floor (default from table config)
        filters:   Extra PostgREST filters e.g. "&domain=eq.code"

    Returns: list of matching rows
    """
    if table not in SEMANTIC_TABLES:
        raise ValueError(f"Table '{table}' not in SEMANTIC_TABLES. Add it first.")

    cfg   = SEMANTIC_TABLES[table]
    thresh = threshold or cfg["threshold"]

    if not query or len(query.strip()) < 2:
        return []

    vec = _embed_safe(query.strip())

    if vec:
        # ── Primary: vector RPC ───────────────────────────────────────────
        try:
            payload = {
                "query_embedding": vec,
                "match_threshold": thresh,
                "match_count": limit,
            }
            if filters:
                payload["extra_filters"] = filters
            r = httpx.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{cfg['rpc']}",
                headers={**_sbh(True), "Prefer": "return=representation"},
                json=payload,
                timeout=15,
            )
            if r.is_success:
                results = r.json()
                if results:
                    print(f"[SEMANTIC] {table} vector: {len(results)} results for '{query[:50]}'")
                    return results[:limit]
        except Exception as e:
            print(f"[SEMANTIC] {table} RPC failed: {e}")

    # ── Emergency ilike fallback (Voyage down) ────────────────────────────
    print(f"[SEMANTIC] WARNING: falling back to ilike for {table} — embed failed")
    return _ilike_fallback(table, query, limit, filters)


def _ilike_fallback(table: str, query: str, limit: int, filters: str = "") -> list:
    """Last-resort ilike. Should rarely fire. Logged always."""
    cfg = SEMANTIC_TABLES[table]
    q = query.strip().replace("'", "").replace('"', "")[:80]
    words = q.split()
    kw = words[0] if words else q
    text_cols = {
        "knowledge_base":    ["content", "topic", "instruction"],
        "mistakes":          ["what_failed", "context", "root_cause"],
        "behavioral_rules":  ["full_rule", "trigger"],
        "pattern_frequency": ["pattern_key"],
        "hot_reflections":   ["reflection_text", "task_summary"],
        "output_reflections":["gap", "new_behavior"],
        "evolution_queue":   ["change_summary", "pattern_key"],
    }.get(table, ["content"])
    or_clause = ",".join(f"{c}.ilike.*{kw}*" for c in text_cols)
    qs = f"select={cfg['select']}&or=({or_clause})&limit={limit}{filters}"
    return sb_get(table, qs, svc=True) or []

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-EMBED ON INSERT
# ══════════════════════════════════════════════════════════════════════════════

def embed_on_insert(table: str, row_id: int, text: str) -> bool:
    """
    Call after every sb_post() to embed the new row immediately.
    Best-effort — never blocks the insert.
    """
    if table not in SEMANTIC_TABLES:
        return False
    if not text or not text.strip():
        return False
    try:
        vec = _get_embedding(text.strip())
        ok = sb_patch(table, f"id=eq.{row_id}", {"embedding": vec})
        if ok:
            print(f"[SEMANTIC] embedded {table}:{row_id} ({len(vec)} dims)")
        return ok
    except Exception as e:
        print(f"[SEMANTIC] embed_on_insert {table}:{row_id} failed (non-fatal): {e}")
        return False

async def embed_on_insert_async(table: str, row_id: int, text: str) -> bool:
    """Async wrapper — use inside asyncio contexts (L11 pipeline)."""
    return await asyncio.to_thread(embed_on_insert, table, row_id, text)

def get_last_inserted_id(table: str, text_col: str, text_val: str) -> int | None:
    """Helper to get id of just-inserted row for embedding."""
    try:
        slug = text_val.strip()[:100].replace("'", "")
        rows = sb_get(table, f"select=id&{text_col}=ilike.*{slug[:30]}*&order=id.desc&limit=1", svc=True) or []
        return rows[0]["id"] if rows else None
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — embed all existing rows per table
# ══════════════════════════════════════════════════════════════════════════════

def backfill_table(table: str, batch_size: int = 20) -> dict:
    """
    Embed all rows in a table that have no embedding yet.
    Safe to run multiple times — skips already-embedded rows.
    """
    if table not in SEMANTIC_TABLES:
        return {"ok": False, "error": f"Table {table} not registered"}

    cfg = SEMANTIC_TABLES[table]

    # Fetch unembedded rows
    try:
        rows = sb_get(
            table,
            f"select={cfg['select']}&embedding=is.null&order=id.asc&limit={batch_size}",
            svc=True,
        ) or []
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not rows:
        return {"ok": True, "message": f"{table}: all rows embedded", "processed": 0}

    embedded = 0
    errors   = []

    for row in rows:
        rid  = row.get("id")
        text = cfg["text_fn"](row)
        if not text or not text.strip():
            continue
        try:
            vec = _get_embedding(text.strip())
            ok  = sb_patch(table, f"id=eq.{rid}", {"embedding": vec})
            if ok:
                embedded += 1
            else:
                errors.append(f"patch failed id={rid}")
            time.sleep(0.4)
        except Exception as e:
            errors.append(f"id={rid}: {str(e)[:80]}")
            time.sleep(1.0)

    # Check if more remain
    remaining = sb_get(table, "select=id&embedding=is.null&limit=1", svc=True) or []

    print(f"[SEMANTIC] backfill {table}: {embedded}/{len(rows)} embedded, {len(errors)} errors")

    return {
        "ok":       True,
        "table":    table,
        "processed": len(rows),
        "embedded": embedded,
        "errors":   errors[:5],
        "has_more": len(remaining) > 0,
        "message":  f"Run again to continue." if remaining else f"Done.",
    }

def backfill_all(batch_size: int = 20) -> dict:
    """Backfill all registered tables in priority order."""
    results = {}
    priority = [
        "mistakes", "behavioral_rules", "pattern_frequency",
        "hot_reflections", "output_reflections", "evolution_queue",
    ]
    for table in priority:
        print(f"[SEMANTIC] backfilling {table}...")
        results[table] = backfill_table(table, batch_size)
        time.sleep(1)
    return results

# ══════════════════════════════════════════════════════════════════════════════
# DDL GENERATOR — creates embedding column + ivfflat index + RPC per table
# ══════════════════════════════════════════════════════════════════════════════

def generate_ddl() -> list:
    """Generate all DDL statements needed. Run once in Supabase SQL editor."""
    stmts = []

    tables_config = {
        "mistakes":          ("match_mistakes",          "id,domain,what_failed,context,root_cause,how_to_avoid,severity,similarity"),
        "behavioral_rules":  ("match_behavioral_rules",  "id,trigger,pointer,full_rule,domain,priority,confidence,similarity"),
        "pattern_frequency": ("match_patterns",          "id,pattern_key,frequency,domain,description,similarity"),
        "hot_reflections":   ("match_hot_reflections",   "id,domain,task_summary,reflection_text,quality_score,similarity"),
        "output_reflections":("match_output_reflections","id,source,gap,gap_domain,new_behavior,verdict,similarity"),
        "evolution_queue":   ("match_evolutions",        "id,change_type,change_summary,pattern_key,status,confidence,similarity"),
    }

    for table, (rpc_name, return_cols) in tables_config.items():
        # Add embedding column
        stmts.append(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector(1024)")

        # ivfflat index
        lists = 100 if table in ("mistakes", "hot_reflections", "pattern_frequency") else 50
        stmts.append(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_embedding "
            f"ON {table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = {lists})"
        )

        # RPC function
        # Build return table columns for CREATE FUNCTION
        col_defs = {
            "id": "bigint", "domain": "text", "what_failed": "text",
            "context": "text", "root_cause": "text", "how_to_avoid": "text",
            "severity": "text", "trigger": "text", "pointer": "text",
            "full_rule": "text", "priority": "integer", "confidence": "float",
            "pattern_key": "text", "frequency": "integer", "description": "text",
            "task_summary": "text", "reflection_text": "text", "quality_score": "float",
            "source": "text", "gap": "text", "gap_domain": "text",
            "new_behavior": "text", "verdict": "text", "change_type": "text",
            "change_summary": "text", "status": "text", "similarity": "float",
            "instruction": "text", "content": "text", "topic": "text",
        }
        ret_cols = [c.strip() for c in return_cols.split(",")]
        ret_defs = ", ".join(f"{c} {col_defs.get(c, 'text')}" for c in ret_cols)

        # Select columns (exclude similarity — computed)
        sel_cols = [c for c in ret_cols if c != "similarity"]
        sel_str  = ", ".join(f"t.{c}" for c in sel_cols)

        rpc = f"""CREATE OR REPLACE FUNCTION {rpc_name}(
  query_embedding vector(1024),
  match_threshold float DEFAULT 0.72,
  match_count     int   DEFAULT 10
)
RETURNS TABLE ({ret_defs})
LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT {sel_str},
    1 - (t.embedding <=> query_embedding) AS similarity
  FROM {table} t
  WHERE
    t.embedding IS NOT NULL
    AND 1 - (t.embedding <=> query_embedding) >= match_threshold
  ORDER BY t.embedding <=> query_embedding
  LIMIT match_count;
END;
$$"""
        stmts.append(rpc)

    return stmts


if __name__ == "__main__":
    print("=== core_semantic.py DDL ===")
    for s in generate_ddl():
        print(s[:80] + "...")
        print()
