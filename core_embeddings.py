"""core_embeddings.py — CORE AGI Semantic Embedding Layer (P3-01 / P3-04)
=========================================================================
Provides vector embeddings via Voyage AI.
Primary:  voyage-3      (1024-dim, high quality — Tier 1)
Fallback: voyage-3-lite (1024-dim, lighter — same dim, safe fallback)
Used by:
  - t_embed_kb_entry        — embed one KB entry and store in knowledge_base.embedding
  - t_semantic_kb_search    — cosine similarity search via pgvector <=> operator
  - t_backfill_kb_embeddings— batch embed all existing KB entries (one-time migration)
  - _embed_episode          — embed conversation episode summaries (P3-04)
  - t_semantic_episode_search — retrieve past episodes relevant to current message

SUPABASE DDL REQUIRED (run manually once):
  -- P3-01: add embedding column to knowledge_base
  ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(1024);
  CREATE INDEX IF NOT EXISTS idx_kb_embedding
    ON knowledge_base USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

  -- P3-04: create conversation_episodes table
  CREATE TABLE IF NOT EXISTS conversation_episodes (
      id          BIGSERIAL PRIMARY KEY,
      chat_id     TEXT NOT NULL,
      summary     TEXT NOT NULL,
      embedding   vector(1024),
      turn_start  TIMESTAMPTZ,
      turn_end    TIMESTAMPTZ,
      topic_tags  TEXT[],
      created_at  TIMESTAMPTZ DEFAULT NOW()
  );
  CREATE INDEX IF NOT EXISTS idx_episodes_chat
    ON conversation_episodes(chat_id);
  CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON conversation_episodes USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);

Depends on: core_config (SUPABASE_URL, _sbh, sb_get, sb_patch, sb_post)
Env vars: VOYAGE_API_KEY (required)
"""
import json
import time
from datetime import datetime

import httpx

from core_config import (
    SUPABASE_URL, SUPABASE_REF, SUPABASE_PAT,
    sb_get, sb_post, sb_patch,
    _sbh,
)

# ── Constants ──────────────────────────────────────────────────────────────────
_EMBED_MODEL          = "voyage-3"       # Voyage AI primary — 1024-dim, Tier 1
_EMBED_MODEL_FALLBACK = "voyage-3-lite"  # Voyage AI fallback — 1024-dim, lighter
_EMBED_DIM     = 1024              # Both voyage-3 and voyage-3-lite output dimension
_EMBED_BATCH   = 20                # entries per backfill batch
_EMBED_SLEEP   = 1.0               # seconds between batches (rate-limit buffer)

import os as _os
_VOYAGE_API_KEY = _os.environ.get("VOYAGE_API_KEY", "")


# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDING API
# ══════════════════════════════════════════════════════════════════════════════

def _voyage_embed(text: str, model: str) -> list:
    """Call Voyage AI embeddings API for a given model. Returns vector or raises."""
    voyage_key = _os.environ.get("VOYAGE_API_KEY", "") or _VOYAGE_API_KEY
    if not voyage_key:
        raise RuntimeError("VOYAGE_API_KEY not set")
    r = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {voyage_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "input": [text]},
        timeout=15,
    )
    if r.status_code == 429:
        print(f"[EMBED] Voyage 429 on {model} — waiting 10s then retry")
        time.sleep(10)
        r = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {voyage_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "input": [text]},
            timeout=15,
        )
        if r.status_code == 429:
            raise Exception(f"Voyage {model} 429 after retry")
    r.raise_for_status()
    values = r.json()["data"][0]["embedding"]
    if not values:
        raise ValueError(f"empty embedding returned from {model}")
    return values


def _get_embedding(text: str) -> list:
    """Return embedding vector for text.
    Primary:  voyage-3      (1024-dim, Tier 1 quality)
    Fallback: voyage-3-lite (1024-dim, same dim — safe DB-compatible fallback)
    Raises RuntimeError if both fail.
    """
    text = text.strip()[:8000]
    last_err = None

    # ── Primary: voyage-3 ─────────────────────────────────────────────────────
    try:
        return _voyage_embed(text, _EMBED_MODEL)
    except Exception as e:
        last_err = f"voyage-3: {e}"
        print(f"[EMBED] voyage-3 failed: {e} — falling back to voyage-3-lite")

    # ── Fallback: voyage-3-lite ───────────────────────────────────────────────
    try:
        return _voyage_embed(text, _EMBED_MODEL_FALLBACK)
    except Exception as e:
        last_err = f"voyage-3-lite: {e}"
        print(f"[EMBED] voyage-3-lite fallback failed: {e}")

    raise RuntimeError(f"All Voyage embedding attempts failed. Last: {last_err}")


def _embed_text_safe(text: str) -> list:
    """Return embedding or empty list on failure (non-blocking)."""
    try:
        return _get_embedding(text)
    except Exception as e:
        print(f"[EMBED] _embed_text_safe failed: {e}")
        return []


def _kb_text(row: dict) -> str:
    """Build the text to embed for a KB entry (topic + instruction + content)."""
    parts = []
    if row.get("topic"):      parts.append(row["topic"])
    if row.get("instruction"): parts.append(row["instruction"][:500])
    if row.get("content"):    parts.append(row["content"][:500])
    return " | ".join(p for p in parts if p).strip()


# ══════════════════════════════════════════════════════════════════════════════
# P3-01: KB EMBEDDING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def t_embed_kb_entry(kb_id: str = "") -> dict:
    """P3-01: Embed a single knowledge_base entry by id.
    Calls voyage-3 (fallback: voyage-3-lite), stores result in knowledge_base.embedding column.
    REQUIRES: knowledge_base.embedding vector(1024) column to exist.
    Returns: {ok, id, dims, topic}
    """
    if not kb_id:
        return {"ok": False, "error": "kb_id required"}
    try:
        rows = sb_get(
            "knowledge_base",
            f"select=id,topic,instruction,content&id=eq.{kb_id}&limit=1",
            svc=True,
        ) or []
        if not rows:
            return {"ok": False, "error": f"KB entry {kb_id} not found"}
        row = rows[0]
        text = _kb_text(row)
        if not text:
            return {"ok": False, "error": "No text content to embed"}

        vec = _get_embedding(text)
        ok = sb_patch("knowledge_base", f"id=eq.{kb_id}",
                      {"embedding": vec, "updated_at": datetime.utcnow().isoformat()})
        return {
            "ok":    ok,
            "id":    kb_id,
            "dims":  len(vec),
            "topic": row.get("topic", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_semantic_kb_search(query: str = "", domain: str = "",
                          limit: str = "10", threshold: str = "0.70") -> dict:
    """P3-01: Semantic KB search using vector cosine similarity.
    Falls back to ilike if pgvector not available or embedding returns empty.
    REQUIRES: knowledge_base.embedding vector(1024) column + ivfflat index.
    Returns same format as t_search_kb for drop-in compatibility.
    """
    if not query:
        return {"ok": False, "error": "query required"}
    lim   = int(limit) if limit else 10
    thresh = float(threshold) if threshold else 0.70

    try:
        vec = _embed_text_safe(query.strip())
        if not vec:
            raise ValueError("empty embedding — falling back to ilike")

        vec_str = "[" + ",".join(str(v) for v in vec) + "]"
        domain_filter = f"&domain=eq.{domain}" if domain and domain not in ("all", "") else ""

        # pgvector cosine similarity via PostgREST RPC
        # Uses the match_knowledge_base function (see DDL note below)
        # If RPC not available, falls back to raw filter
        try:
            r = httpx.post(
                f"{SUPABASE_URL}/rest/v1/rpc/match_knowledge_base",
                headers={**_sbh(True), "Prefer": "return=representation"},
                json={
                    "query_embedding": vec,
                    "match_threshold": thresh,
                    "match_count":     lim,
                    **({"filter_domain": domain} if domain and domain not in ("all", "") else {}),
                },
                timeout=15,
            )
            if r.is_success:
                rows = r.json()
                return {"ok": True, "mode": "semantic", "count": len(rows), "results": rows}
        except Exception as rpc_err:
            print(f"[EMBED] RPC match_knowledge_base failed: {rpc_err} — trying direct query")

        # Direct PostgREST vector query (works without custom RPC if pgvector installed)
        qs = (
            f"select=id,domain,topic,instruction,content,confidence"
            f"&active=eq.true&id=gt.1"
            f"{domain_filter}"
            f"&embedding=not.is.null"
            f"&limit={lim}"
        )
        # PostgREST does not support vector ordering - this query would 400
        # Remove qs entirely, raise to trigger ilike fallback
        raise ValueError("Direct vector query not supported — use RPC")

    except Exception as e:
        print(f"[EMBED] semantic search failed ({e}), falling back to ilike")
        # Graceful fallback to keyword search
        try:
            from core_tools import t_search_kb
            rows = t_search_kb(query=query, domain=domain, limit=lim)
            return {"ok": True, "mode": "ilike_fallback", "count": len(rows), "results": rows}
        except Exception as fe:
            return {"ok": False, "error": str(fe)}


def t_backfill_kb_embeddings(batch_size: str = "20", domain: str = "") -> dict:
    """P3-01: Batch embed all knowledge_base entries that have no embedding yet.
    Processes in batches, respects rate limits. Safe to run multiple times.
    Returns: {ok, processed, embedded, skipped, errors}
    """
    try:
        bs = min(int(batch_size) if batch_size else 20, 50)
        domain_filter = f"&domain=eq.{domain}" if domain and domain not in ("all", "") else ""

        # Fetch rows without embedding (embedding IS NULL)
        rows = sb_get(
            "knowledge_base",
            f"select=id,topic,instruction,content&active=eq.true&id=gt.1"
            f"&embedding=is.null{domain_filter}"
            f"&order=id.asc&limit={bs}",
            svc=True,
        ) or []

        if not rows:
            return {"ok": True, "processed": 0, "embedded": 0,
                    "message": "All entries already embedded or no entries found"}

        embedded = 0
        skipped  = 0
        errors   = []

        for row in rows:
            rid  = row["id"]
            text = _kb_text(row)
            if not text:
                skipped += 1
                continue
            try:
                vec = _get_embedding(text)
                ok  = sb_patch("knowledge_base", f"id=eq.{rid}",
                               {"embedding": vec, "updated_at": datetime.utcnow().isoformat()})
                if ok:
                    embedded += 1
                else:
                    errors.append(f"patch failed for id={rid}")
                time.sleep(0.5)  # Tier 1 rate limit buffer
            except Exception as e:
                errors.append(f"id={rid}: {str(e)[:80]}")
                time.sleep(_EMBED_SLEEP)

        remaining_count = 0
        try:
            remaining = sb_get(
                "knowledge_base",
                f"select=id&active=eq.true&id=gt.1&embedding=is.null{domain_filter}&limit=1",
                svc=True,
            ) or []
            remaining_count = len(remaining)
        except Exception:
            pass

        return {
            "ok":             True,
            "batch_size":     bs,
            "processed":      len(rows),
            "embedded":       embedded,
            "skipped":        skipped,
            "errors":         errors[:5],
            "has_more":       remaining_count > 0,
            "message":        f"Embedded {embedded}/{len(rows)}. Run again to continue." if remaining_count else f"Done. Embedded {embedded}/{len(rows)}.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ??????????????????????????????????????????????????????????????????????????????
SEMANTIC_SCHEMA_DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector;",
    """
    CREATE TABLE IF NOT EXISTS knowledge_base (
      id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY,
      domain text NOT NULL DEFAULT 'general',
      topic text NOT NULL DEFAULT '',
      content text NOT NULL DEFAULT '',
      source text,
      confidence text DEFAULT 'medium',
      tags text[],
      project_id integer,
      created_at timestamptz DEFAULT NOW(),
      updated_at timestamptz DEFAULT NOW(),
      instruction text,
      source_type text,
      source_ref text,
      source_ts timestamptz,
      active boolean DEFAULT true,
      access_count integer DEFAULT 0,
      last_accessed timestamptz,
      embedding vector(1024)
    );
    """,
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT 'general';",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS topic text NOT NULL DEFAULT '';",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS content text NOT NULL DEFAULT '';",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source text;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS confidence text DEFAULT 'medium';",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS tags text[];",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS project_id integer;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT NOW();",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT NOW();",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS instruction text;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_type text;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_ref text;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_ts timestamptz;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS active boolean DEFAULT true;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS access_count integer DEFAULT 0;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS last_accessed timestamptz;",
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(1024);",
    "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_base_domain_topic_key ON knowledge_base (domain, topic);",
    "CREATE INDEX IF NOT EXISTS idx_kb_domain ON knowledge_base USING btree (domain);",
    "CREATE INDEX IF NOT EXISTS idx_kb_embedding ON knowledge_base USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);",
    "CREATE INDEX IF NOT EXISTS knowledge_base_access_idx ON knowledge_base USING btree (access_count DESC);",
    "CREATE INDEX IF NOT EXISTS knowledge_base_last_accessed_idx ON knowledge_base USING btree (last_accessed DESC);",
    """
    CREATE TABLE IF NOT EXISTS conversation_episodes (
      id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL PRIMARY KEY,
      chat_id text NOT NULL DEFAULT '',
      summary text NOT NULL DEFAULT '',
      turn_start timestamptz,
      turn_end timestamptz,
      topic_tags text[],
      created_at timestamptz DEFAULT NOW(),
      embedding vector(1024)
    );
    """,
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS chat_id text NOT NULL DEFAULT '';",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS turn_start timestamptz;",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS turn_end timestamptz;",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS topic_tags text[];",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT NOW();",
    "ALTER TABLE conversation_episodes ADD COLUMN IF NOT EXISTS embedding vector(1024);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_chat ON conversation_episodes(chat_id);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_embedding ON conversation_episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);",
    """
    CREATE OR REPLACE FUNCTION match_knowledge_base(
      query_embedding vector(1024),
      match_threshold float DEFAULT 0.70,
      match_count int DEFAULT 10,
      filter_domain text DEFAULT NULL
    )
    RETURNS TABLE (
      id bigint,
      domain text,
      topic text,
      instruction text,
      content text,
      confidence text,
      similarity float
    )
    LANGUAGE plpgsql AS $$
    BEGIN
      RETURN QUERY
      SELECT
        kb.id, kb.domain, kb.topic, kb.instruction, kb.content, kb.confidence,
        1 - (kb.embedding <=> query_embedding) AS similarity
      FROM knowledge_base kb
      WHERE
        kb.active = true
        AND kb.id > 1
        AND kb.embedding IS NOT NULL
        AND (filter_domain IS NULL OR kb.domain = filter_domain)
        AND 1 - (kb.embedding <=> query_embedding) >= match_threshold
      ORDER BY kb.embedding <=> query_embedding
      LIMIT match_count;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION match_conversation_episodes(
      query_embedding vector(1024),
      match_threshold float DEFAULT 0.65,
      match_count int DEFAULT 3,
      filter_chat_id text DEFAULT NULL
    )
    RETURNS TABLE (
      id bigint,
      summary text,
      turn_start timestamptz,
      topic_tags text[],
      similarity float
    )
    LANGUAGE plpgsql AS $$
    BEGIN
      RETURN QUERY
      SELECT
        ce.id, ce.summary, ce.turn_start, ce.topic_tags,
        1 - (ce.embedding <=> query_embedding) AS similarity
      FROM conversation_episodes ce
      WHERE
        ce.embedding IS NOT NULL
        AND (filter_chat_id IS NULL OR ce.chat_id = filter_chat_id)
        AND 1 - (ce.embedding <=> query_embedding) >= match_threshold
      ORDER BY ce.embedding <=> query_embedding
      LIMIT match_count;
    END;
    $$;
    """,
]


def semantic_schema_ddl() -> list[str]:
    """Return the semantic-memory bootstrap SQL statements."""
    return [stmt.strip() for stmt in SEMANTIC_SCHEMA_DDL if stmt.strip()]





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


def apply_semantic_schema() -> dict:
    """Apply the semantic-memory bootstrap through the Supabase management API."""
    if not SUPABASE_PAT:
        return {"ok": False, "error": "SUPABASE_PAT not set"}
    try:
        results = []
        errors = []
        for stmt in semantic_schema_ddl():
            attempts = 0
            while True:
                attempts += 1
                resp = httpx.post(
                    f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
                    headers={
                        "Authorization": f"Bearer {SUPABASE_PAT}",
                        "Content-Type": "application/json",
                    },
                    json={"query": stmt if stmt.rstrip().endswith(';') else stmt + ';'},
                    timeout=60,
                )
                ok = resp.status_code in (200, 201)
                results.append({"ok": ok, "status_code": resp.status_code})
                if ok:
                    break
                err = resp.text[:300]
                if attempts < 12 and _is_transient_supabase_error(err):
                    wait = min(30, 2 ** attempts)
                    print(f"[SEMANTIC_SCHEMA] transient error, retrying in {wait}s: {resp.status_code} {err}")
                    time.sleep(wait)
                    continue
                print(f"[SEMANTIC_SCHEMA] DDL failed: {resp.status_code} {err}")
                errors.append(err)
                break
        reload_attempts = 0
        while True:
            reload_attempts += 1
            reload_resp = httpx.post(
                f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
                headers={
                    "Authorization": f"Bearer {SUPABASE_PAT}",
                    "Content-Type": "application/json",
                },
                json={"query": "SELECT pg_notify('pgrst', 'reload schema');"},
                timeout=30,
            )
            results.append({"ok": reload_resp.status_code in (200, 201), "status_code": reload_resp.status_code})
            if reload_resp.status_code in (200, 201):
                break
            err = reload_resp.text[:300]
            if reload_attempts < 12 and _is_transient_supabase_error(err):
                wait = min(30, 2 ** reload_attempts)
                print(f"[SEMANTIC_SCHEMA] transient reload error, retrying in {wait}s: {reload_resp.status_code} {err}")
                time.sleep(wait)
                continue
            errors.append(err)
            break
        return {
            "ok": not errors,
            "bootstrapped": not errors,
            "results": results[:12],
            "errors": errors[:8],
        }
    except Exception as e:
        print(f"[SEMANTIC_SCHEMA] DDL error: {e}")
        return {"ok": False, "error": str(e)}


# P3-04: CONVERSATION EPISODE MEMORY
def compress_to_episode(cid: str, turns: list) -> dict:
    """Compress a list of conversation turns into an episode summary with embedding.
    Called by _compress_history when conversation exceeds threshold.
    turns: list of {role, content, ts} dicts.
    Returns: {ok, episode_id, summary, dims}
    """
    if not turns:
        return {"ok": False, "error": "no turns provided"}
    try:
        from core_config import gemini_chat
        # Build turn text for summarization
        turn_text = "\n".join(
            f"{t.get('role','?').upper()}: {t.get('content','')[:200]}"
            for t in turns
        )
        summary = gemini_chat(
            system=(
                "Summarize this conversation segment in 3-4 sentences. "
                "Focus on: what was asked, what was done, key outcomes, any open items. "
                "Be specific — include tool names, file names, task IDs if mentioned."
            ),
            user=turn_text[:4000],
            max_tokens=300,
        )
        if not summary or len(summary) < 10:
            summary = f"Conversation segment: {len(turns)} turns"

        # Generate embedding
        vec = _embed_text_safe(summary)

        # Extract timestamps
        ts_start = turns[0].get("ts",  "") if turns else ""
        ts_end   = turns[-1].get("ts", "") if turns else ""

        # Extract topic tags (simple keyword extraction)
        combined = " ".join(t.get("content", "") for t in turns)
        _TOPIC_KW = ["deploy", "patch", "railway", "supabase", "task", "kb",
                     "lsei", "rmu", "mltx", "training", "evolution", "tool",
                     "error", "fix", "project", "session"]
        tags = [kw for kw in _TOPIC_KW if kw in combined.lower()][:8]

        row = {
            "chat_id":    cid,
            "summary":    summary,
            "turn_start": ts_start or None,
            "turn_end":   ts_end   or None,
            "topic_tags": tags,
            "created_at": datetime.utcnow().isoformat(),
        }
        if vec:
            row["embedding"] = vec

        ok = sb_post("conversation_episodes", row)
        if not ok:
            return {"ok": False, "error": "failed to insert episode"}

        # Get the new episode id
        ep_rows = sb_get(
            "conversation_episodes",
            f"select=id&chat_id=eq.{cid}&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        ep_id = ep_rows[0]["id"] if ep_rows else None

        return {
            "ok":        True,
            "episode_id": ep_id,
            "summary":   summary,
            "dims":      len(vec),
            "tags":      tags,
        }
    except Exception as e:
        print(f"[P3-04] compress_to_episode error: {e}")
        return {"ok": False, "error": str(e)}


def retrieve_relevant_episodes(cid: str, message: str, limit: int = 3) -> list:
    """P3-04: Retrieve past conversation episodes semantically relevant to current message.
    Returns list of {summary, turn_start, topic_tags} for injection into system prompt.
    Falls back to recent episodes if embedding unavailable.
    """
    try:
        vec = _embed_text_safe(message.strip())

        if vec:
            # Try RPC first, then direct query
            try:
                r = httpx.post(
                    f"{SUPABASE_URL}/rest/v1/rpc/match_conversation_episodes",
                    headers={**_sbh(True), "Prefer": "return=representation"},
                    json={
                        "query_embedding": vec,
                        "match_threshold": 0.65,
                        "match_count":     limit,
                        "filter_chat_id":  cid,
                    },
                    timeout=10,
                )
                if r.is_success:
                    return r.json()[:limit]
            except Exception:
                pass

            # Direct vector query fallback
            rows = sb_get(
                "conversation_episodes",
                f"select=id,summary,turn_start,topic_tags"
                f"&chat_id=eq.{cid}&embedding=not.is.null"
                f"&order=created_at.desc&limit={limit * 3}",
                svc=True,
            ) or []
            # Return most recent if can't do similarity (no RPC)
            return rows[:limit]

        else:
            # No embedding — return most recent episodes
            rows = sb_get(
                "conversation_episodes",
                f"select=id,summary,turn_start,topic_tags"
                f"&chat_id=eq.{cid}&order=created_at.desc&limit={limit}",
                svc=True,
            ) or []
            return rows

    except Exception as e:
        print(f"[P3-04] retrieve_relevant_episodes error: {e}")
        return []


def t_semantic_episode_search(chat_id: str = "", query: str = "", limit: str = "3") -> dict:
    """P3-04: Search past conversation episodes by semantic similarity.
    chat_id: Telegram chat ID to scope search.
    query: what to find in past conversations.
    Returns: {ok, episodes: [{summary, turn_start, topic_tags}], count}
    """
    if not chat_id or not query:
        return {"ok": False, "error": "chat_id and query required"}
    try:
        episodes = retrieve_relevant_episodes(chat_id, query, int(limit) if limit else 3)
        return {"ok": True, "episodes": episodes, "count": len(episodes)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE RPC DDL (for reference — run in Supabase SQL editor)
# ══════════════════════════════════════════════════════════════════════════════
"""
-- REQUIRED: enable pgvector extension first
CREATE EXTENSION IF NOT EXISTS vector;

-- P3-01: KB embedding column
ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(1024);
CREATE INDEX IF NOT EXISTS idx_kb_embedding
  ON knowledge_base USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- P3-01: RPC function for cosine similarity search
CREATE OR REPLACE FUNCTION match_knowledge_base(
  query_embedding vector(1024),
  match_threshold float DEFAULT 0.70,
  match_count     int   DEFAULT 10,
  filter_domain   text  DEFAULT NULL
)
RETURNS TABLE (
  id          bigint,
  domain      text,
  topic       text,
  instruction text,
  content     text,
  confidence  text,
  similarity  float
)
LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT
    kb.id, kb.domain, kb.topic, kb.instruction, kb.content, kb.confidence,
    1 - (kb.embedding <=> query_embedding) AS similarity
  FROM knowledge_base kb
  WHERE
    kb.active = true
    AND kb.id > 1
    AND kb.embedding IS NOT NULL
    AND (filter_domain IS NULL OR kb.domain = filter_domain)
    AND 1 - (kb.embedding <=> query_embedding) >= match_threshold
  ORDER BY kb.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- P3-04: conversation_episodes table
CREATE TABLE IF NOT EXISTS conversation_episodes (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     TEXT NOT NULL,
  summary     TEXT NOT NULL,
  embedding   vector(1024),
  turn_start  TIMESTAMPTZ,
  turn_end    TIMESTAMPTZ,
  topic_tags  TEXT[],
  created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_episodes_chat
  ON conversation_episodes(chat_id);
CREATE INDEX IF NOT EXISTS idx_episodes_embedding
  ON conversation_episodes USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 50);

-- P3-04: RPC function for episode similarity search
CREATE OR REPLACE FUNCTION match_conversation_episodes(
  query_embedding vector(1024),
  match_threshold float DEFAULT 0.65,
  match_count     int   DEFAULT 3,
  filter_chat_id  text  DEFAULT NULL
)
RETURNS TABLE (
  id         bigint,
  summary    text,
  turn_start timestamptz,
  topic_tags text[],
  similarity float
)
LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT
    ce.id, ce.summary, ce.turn_start, ce.topic_tags,
    1 - (ce.embedding <=> query_embedding) AS similarity
  FROM conversation_episodes ce
  WHERE
    ce.embedding IS NOT NULL
    AND (filter_chat_id IS NULL OR ce.chat_id = filter_chat_id)
    AND 1 - (ce.embedding <=> query_embedding) >= match_threshold
  ORDER BY ce.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;
"""
