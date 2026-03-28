"""
core_semantic.py — CORE Unified Semantic Search Layer
======================================================
Single entry point for ALL meaning-based searches across CORE's tables.
Replaces ilike in every search function. ilike is REMOVED, not fallback.

Only exception: Voyage AI outage -> emergency ilike fallback (logged, not silent).

Tables covered:
  knowledge_base     — primary KB (L2 + t_search_kb)
  mistakes           — t_search_mistakes + predict_failure + log_mistake dedup
  behavioral_rules   — semantic rule load
  pattern_frequency  — dedup before cold processor
  hot_reflections    — cold processor semantic cluster
  output_reflections — L11 meta evaluator dedup
  evolution_queue    — evo dedup before inserting
  conversation_episodes — episode memory + session retrieval
  repo_components    — semantic repository map
  repo_component_chunks — semantic repo chunks for retrieval
  repo_component_edges — semantic repo wiring graph
"""
import hashlib
import time
import asyncio
from datetime import datetime
from typing import Optional

import httpx
from core_config import SUPABASE_URL, SUPABASE_REF, SUPABASE_PAT, _sbh, sb_get, sb_post, sb_patch

# ── Constants ──────────────────────────────────────────────────────────────────
_EMBED_DIM        = 1024
_DEFAULT_THRESHOLD = 0.20
_MGMT_URL         = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"
_MGMT_HEADERS     = {"Authorization": f"Bearer {SUPABASE_PAT}", "Content-Type": "application/json"}

# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDING — delegate to core_embeddings (battle-tested, env-aware)
# ══════════════════════════════════════════════════════════════════════════════

def _get_embedding(text: str) -> list:
    """Get embedding via core_embeddings (Voyage AI voyage-3, fallback voyage-3-lite)."""
    from core_embeddings import _get_embedding as _ce_embed
    return _ce_embed(text)

def _embed_safe(text: str) -> list:
    """Non-raising embed. Returns [] on any failure."""
    from core_embeddings import _embed_text_safe
    return _embed_text_safe(text)

# ══════════════════════════════════════════════════════════════════════════════
# TABLE REGISTRY
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

def _episode_text(r):
    parts = [
        r.get("summary",""),
        r.get("chat_id",""),
    ]
    tags = r.get("topic_tags") or []
    if isinstance(tags, list):
        parts.extend(tags)
    return " | ".join(p for p in parts if p)

def _repo_component_text(r):
    parts = [
        r.get("path",""),
        r.get("runtime_role",""),
        r.get("language",""),
        r.get("summary",""),
        r.get("purpose_summary",""),
    ]
    symbols = r.get("symbols") or {}
    if isinstance(symbols, dict):
        parts.extend(symbols.get("functions") or [])
        parts.extend(symbols.get("classes") or [])
        parts.extend(symbols.get("headings") or [])
        parts.extend(symbols.get("keys") or [])
    parts.extend(r.get("imports") or [])
    parts.extend(r.get("links") or [])
    return " | ".join(str(p) for p in parts if p)

def _repo_chunk_text(r):
    return " | ".join(p for p in [
        r.get("component_path",""),
        f"chunk:{r.get('chunk_index','')}",
        r.get("summary",""),
        r.get("content",""),
    ] if p)

def _repo_edge_text(r):
    return " | ".join(p for p in [
        r.get("source_path",""),
        r.get("target_path",""),
        r.get("relation",""),
        r.get("source_symbol",""),
        r.get("target_symbol",""),
        r.get("evidence",""),
    ] if p)

SEMANTIC_TABLES = {
    "knowledge_base": {
        "text_fn":   _kb_text,
        "rpc":       "match_knowledge_base",
        "select":    "id,domain,topic,instruction,content,confidence",
        "threshold": 0.20,
    },
    "mistakes": {
        "text_fn":   _mistake_text,
        "rpc":       "match_mistakes",
        "select":    "id,domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity",
        "threshold": 0.20,
    },
    "behavioral_rules": {
        "text_fn":   _rule_text,
        "rpc":       "match_behavioral_rules",
        "select":    "id,trigger,pointer,full_rule,domain,priority,confidence",
        "threshold": 0.20,
    },
    "pattern_frequency": {
        "text_fn":   _pattern_text,
        "rpc":       "match_patterns",
        "select":    "id,pattern_key,frequency,domain,description",
        "threshold": 0.20,
    },
    "hot_reflections": {
        "text_fn":   _hot_text,
        "rpc":       "match_hot_reflections",
        "select":    "id,domain,task_summary,reflection_text,gaps_identified,quality_score",
        "threshold": 0.20,
    },
    "output_reflections": {
        "text_fn":   _reflect_text,
        "rpc":       "match_output_reflections",
        "select":    "id,source,gap,gap_domain,new_behavior,verdict",
        "threshold": 0.20,
    },
    "evolution_queue": {
        "text_fn":   _evo_text,
        "rpc":       "match_evolutions",
        "select":    "id,change_type,change_summary,pattern_key,status,confidence",
        "threshold": 0.20,
    },
    "conversation_episodes": {
        "text_fn":   _episode_text,
        "rpc":       "match_conversation_episodes",
        "select":    "id,chat_id,summary,topic_tags,embedding",
        "threshold": 0.20,
    },
    "repo_components": {
        "text_fn":   _repo_component_text,
        "rpc":       "match_repo_components",
        "select":    "id,repo,path,file_name,file_ext,language,item_type,runtime_role,summary,purpose_summary,symbols,imports,links,file_hash,content_hash",
        "threshold": 0.20,
    },
    "repo_component_chunks": {
        "text_fn":   _repo_chunk_text,
        "rpc":       "match_repo_component_chunks",
        "select":    "id,repo,component_path,chunk_index,chunk_type,start_line,end_line,summary,content,chunk_hash",
        "threshold": 0.20,
    },
    "repo_component_edges": {
        "text_fn":   _repo_edge_text,
        "rpc":       "match_repo_component_edges",
        "select":    "id,repo,source_path,target_path,relation,source_symbol,target_symbol,evidence,weight",
        "threshold": 0.20,
    },
}

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
    Replaces ALL ilike searches. Emergency ilike fallback only if Voyage is down.
    """
    if table not in SEMANTIC_TABLES:
        raise ValueError(f"Table '{table}' not in SEMANTIC_TABLES")

    cfg    = SEMANTIC_TABLES[table]
    thresh = threshold if threshold is not None else cfg["threshold"]

    if not query or len(query.strip()) < 2:
        return []

    vec = _embed_safe(query.strip())

    if vec:
        try:
            payload = {
                "query_embedding": vec,
                "match_threshold": thresh,
                "match_count":     limit,
            }
            r = httpx.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{cfg['rpc']}",
                headers={**_sbh(True), "Prefer": "return=representation"},
                json=payload,
                timeout=15,
            )
            if r.is_success:
                results = r.json()
                if results:
                    print(f"[SEMANTIC] {table}: {len(results)} results for '{query[:50]}'")
                    return results[:limit]
                # RPC succeeded but 0 results — may be backfill still in progress
                # Fall through to ilike only if vec is valid
        except Exception as e:
            print(f"[SEMANTIC] {table} RPC failed: {e}")

    # Emergency ilike fallback
    print(f"[SEMANTIC] WARNING: falling back to ilike for {table} — embed failed")
    return _ilike_fallback(table, query, limit, filters)


def search_many(
    query: str,
    tables: list[str] | None = None,
    limit: int = 10,
    domain: str = "",
    filters_by_table: dict[str, str] | None = None,
) -> list:
    """Fan out one semantic query across multiple registered tables and merge the results.

    This is the unified memory read path CORE should use when it wants both the KB and
    the native semantic tables in a single reasoning call.
    """
    if not query or len(query.strip()) < 2:
        return []
    tables = tables or list(SEMANTIC_TABLES.keys())
    filters_by_table = filters_by_table or {}
    per_table_limit = max(1, min(limit, 5))
    merged = []
    seen = set()
    for table in tables:
        if table not in SEMANTIC_TABLES:
            continue
        table_filters = filters_by_table.get(table, "")
        if not table_filters and domain and domain not in ("", "all"):
            if table in {"knowledge_base", "mistakes", "behavioral_rules", "pattern_frequency", "hot_reflections", "output_reflections", "evolution_queue"}:
                table_filters = f"&domain=eq.{domain}"
        try:
            rows = search(table, query, limit=per_table_limit, filters=table_filters) or []
        except Exception:
            rows = []
        for row in rows:
            rid = row.get("id") or row.get("event_id") or row.get("topic") or row.get("pattern_key") or row.get("change_summary") or row.get("summary")
            key = (table, str(rid))
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["semantic_table"] = table
            item["semantic_query"] = query
            item["semantic_score"] = row.get("score") or row.get("similarity") or row.get("match_score") or row.get("_score") or 0
            merged.append(item)
    merged.sort(key=lambda r: float(r.get("semantic_score") or 0), reverse=True)
    return merged[:limit]


def _ilike_fallback(table: str, query: str, limit: int, filters: str = "") -> list:
    """Last-resort ilike. Should rarely fire in production (only on Voyage outage)."""
    cfg = SEMANTIC_TABLES[table]
    q   = query.strip().replace("'","").replace('"',"")[:80]
    kw  = q.split()[0] if q.split() else q
    text_cols = {
        "knowledge_base":    ["content","topic","instruction"],
        "mistakes":          ["what_failed","context","root_cause"],
        "behavioral_rules":  ["full_rule","trigger"],
        "pattern_frequency": ["pattern_key"],
        "hot_reflections":   ["reflection_text","task_summary"],
        "output_reflections":["gap","new_behavior"],
        "evolution_queue":   ["change_summary","pattern_key"],
        "conversation_episodes": ["summary","chat_id"],
        "repo_components":   ["path","summary","purpose_summary","file_name","runtime_role"],
        "repo_component_chunks": ["component_path","summary","content"],
        "repo_component_edges": ["source_path","target_path","relation","evidence"],
    }.get(table, ["content"])
    or_clause = ",".join(f"{c}.ilike.*{kw}*" for c in text_cols)
    qs = f"select={cfg['select']}&or=({or_clause})&limit={limit}{filters}"
    return sb_get(table, qs, svc=True) or []

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-EMBED ON INSERT
# ══════════════════════════════════════════════════════════════════════════════

def embed_on_insert(table: str, row_id: int, text: str) -> bool:
    """Call after every sb_post() to embed the new row. Best-effort, never blocks."""
    if table not in SEMANTIC_TABLES or not text or not text.strip():
        return False
    try:
        vec = _get_embedding(text.strip())
        ok  = sb_patch(table, f"id=eq.{row_id}", {"embedding": vec})
        if ok:
            print(f"[SEMANTIC] embedded {table}:{row_id} ({len(vec)} dims)")
        return ok
    except Exception as e:
        print(f"[SEMANTIC] embed_on_insert {table}:{row_id} failed (non-fatal): {e}")
        return False

async def embed_on_insert_async(table: str, row_id: int, text: str) -> bool:
    """Async wrapper for L11 pipeline."""
    return await asyncio.to_thread(embed_on_insert, table, row_id, text)

# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL
# ══════════════════════════════════════════════════════════════════════════════

def backfill_table(table: str, batch_size: int = 20) -> dict:
    """Embed all rows with no embedding yet. Safe to run multiple times."""
    if table not in SEMANTIC_TABLES:
        return {"ok": False, "error": f"Table {table} not registered"}

    cfg = SEMANTIC_TABLES[table]
    try:
        rows = sb_get(
            table,
            f"select={cfg['select']}&embedding=is.null&order=id.asc&limit={batch_size}",
            svc=True,
        ) or []
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not rows:
        return {"ok": True, "message": f"{table}: all rows embedded", "processed": 0,
                "embedded": 0, "errors": [], "has_more": False}

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

    remaining = sb_get(table, "select=id&embedding=is.null&limit=1", svc=True) or []
    print(f"[SEMANTIC] backfill {table}: {embedded}/{len(rows)} embedded, {len(errors)} errors")
    return {
        "ok": True, "table": table, "processed": len(rows),
        "embedded": embedded, "errors": errors[:5],
        "has_more": len(remaining) > 0,
        "message": "Run again to continue." if remaining else "Done.",
    }

def backfill_all(batch_size: int = 20) -> dict:
    """Backfill all registered tables in priority order."""
    results = {}
    for table in ["mistakes","behavioral_rules","pattern_frequency",
                  "hot_reflections","output_reflections","evolution_queue",
                  "conversation_episodes","repo_components","repo_component_chunks","repo_component_edges"]:
        print(f"[SEMANTIC] backfilling {table}...")
        results[table] = backfill_table(table, batch_size)
        time.sleep(1)
    return results

# ══════════════════════════════════════════════════════════════════════════════
# DDL GENERATOR (for reference — already applied)
# ══════════════════════════════════════════════════════════════════════════════

def generate_ddl() -> list:
    """Returns DDL statements (already applied — for reference only)."""
    return ["-- DDL already applied. See fix_rpc_final.py for RPC definitions."]

if __name__ == "__main__":
    print("core_semantic.py — CORE Unified Semantic Search Layer")
    print(f"Registered tables: {list(SEMANTIC_TABLES.keys())}")
