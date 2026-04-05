"""
core_embed_sync.py — Auto-Embed Sync Layer
==========================================
Intercepts sb_post() calls to semantic tables and auto-embeds new rows.
This ensures ALL writes — from core_tools, core_train, core_orch, L11 workers,
background researcher, everywhere — are automatically converted to semantic.

Usage: imported once at core_config level or core_main startup.
       Monkey-patches sb_post with sb_post_with_embed.

Tables covered (SEMANTIC_TABLES from core_semantic):
  knowledge_base, mistakes, behavioral_rules, pattern_frequency,
  hot_reflections, output_reflections, evolution_queue

How it works:
  1. Wraps the original sb_post()
  2. After every successful insert to a semantic table,
     fetches the new row id (by order desc)
  3. Embeds the text content via Voyage AI
  4. Patches the embedding column on the new row
  5. All async, non-blocking — never delays the original write
"""
import sys
import threading
from datetime import datetime
from urllib.parse import quote

# ── Text extractors per table (same as core_semantic SEMANTIC_TABLES) ─────────
_TEXT_FN = {
    "knowledge_base": lambda r: " | ".join(
        p for p in [r.get("topic",""), r.get("instruction",""), r.get("content","")] if p
    ),
    "mistakes": lambda r: " | ".join(
        p for p in [r.get("what_failed",""), r.get("context",""),
                    r.get("root_cause",""), r.get("how_to_avoid","")] if p
    ),
    "behavioral_rules": lambda r: r.get("full_rule","") or r.get("trigger","") or "",
    "pattern_frequency": lambda r: r.get("pattern_key","") or r.get("description","") or "",
    "hot_reflections": lambda r: " | ".join(
        p for p in [r.get("reflection_text",""), r.get("task_summary","")] if p
    ),
    "output_reflections": lambda r: " | ".join(
        p for p in [r.get("gap",""), r.get("new_behavior",""), r.get("gap_domain","")] if p
    ),
    "evolution_queue": lambda r: " | ".join(
        p for p in [r.get("change_summary",""), r.get("pattern_key","")] if p
    ),
    "conversation_episodes": lambda r: " | ".join(
        p for p in [r.get("summary",""), r.get("chat_id",""), " ".join(r.get("topic_tags", []) if isinstance(r.get("topic_tags"), list) else [])] if p
    ),
}

_TEXT_COLUMNS = {
    "knowledge_base": ["topic", "instruction", "content"],
    "mistakes": ["what_failed", "context", "root_cause", "how_to_avoid"],
    "behavioral_rules": ["full_rule", "trigger"],
    "pattern_frequency": ["pattern_key", "description"],
    "hot_reflections": ["reflection_text", "task_summary"],
    "output_reflections": ["gap", "new_behavior", "gap_domain"],
    "evolution_queue": ["change_summary", "pattern_key"],
    "conversation_episodes": ["summary", "chat_id", "topic_tags"],
}

# ── ID fetch queries per table ─────────────────────────────────────────────────
_ID_QUERY = {
    "knowledge_base":    "select=id&order=id.desc&limit=1",
    "mistakes":          "select=id&order=id.desc&limit=1",
    "behavioral_rules":  "select=id&order=id.desc&limit=1",
    "pattern_frequency": "select=id&order=id.desc&limit=1",
    "hot_reflections":   "select=id&order=id.desc&limit=1",
    "output_reflections":"select=id&order=id.desc&limit=1",
    "evolution_queue":   "select=id&order=id.desc&limit=1",
    "conversation_episodes": "select=id&order=id.desc&limit=1",
}

_PATCH_TEXT_KEYS = {
    "knowledge_base": {"topic", "instruction", "content", "tags", "confidence", "source", "source_type", "source_ref"},
    "mistakes": {"domain", "context", "what_failed", "correct_approach", "root_cause", "how_to_avoid", "severity"},
    "behavioral_rules": {"trigger", "pointer", "full_rule", "domain", "priority", "source", "confidence"},
    "pattern_frequency": {"pattern_key", "frequency", "domain", "description", "stale", "auto_applied"},
    "hot_reflections": {"domain", "task_summary", "reflection_text", "gaps_identified", "quality_score"},
    "output_reflections": {"source", "gap", "gap_domain", "new_behavior", "verdict"},
    "evolution_queue": {"change_type", "change_summary", "recommendation", "diff_content", "pattern_key", "impact", "source", "confidence"},
    "conversation_episodes": {"chat_id", "summary", "topic_tags"},
}

_WRAPPED = {"sb_post": False, "sb_post_critical": False, "sb_upsert": False, "sb_patch": False}


def _relevant_patch(table: str, data: dict) -> bool:
    if table not in _PATCH_TEXT_KEYS:
        return False
    if not isinstance(data, dict):
        return False
    keys = set(data.keys())
    if not keys:
        return False
    if keys.issubset({"embedding", "updated_at", "last_accessed", "processed_by_cold", "tier_applied_at"}):
        return False
    return bool(keys & _PATCH_TEXT_KEYS[table])


def _parse_id_from_filters(filters: str) -> str:
    if not filters:
        return ""
    for chunk in filters.split("&"):
        chunk = chunk.strip()
        if chunk.startswith("id=eq."):
            return chunk.split("id=eq.", 1)[1].strip()
        if chunk.startswith("id=in.(") and chunk.endswith(")"):
            inside = chunk[len("id=in.("):-1]
            return inside.split(",")[0].strip()
    return ""


def _rebind_module_globals(name: str, wrapper) -> None:
    for mod in list(sys.modules.values()):
        try:
            if hasattr(mod, name) and getattr(mod, name) is not None:
                setattr(mod, name, wrapper)
        except Exception:
            continue


def _sb_eq_value(value: object, max_len: int | None = None) -> str:
    text = "" if value is None else str(value)
    if max_len is not None:
        text = text[:max_len]
    return quote(text, safe="")


def _select_for_table(table: str) -> str:
    cols = ["id"] + list(_TEXT_COLUMNS.get(table, []))
    seen = []
    for col in cols:
        if col and col not in seen:
            seen.append(col)
    return ",".join(seen) if seen else "id"


def _embed_row(table: str, row: dict, row_id: str | None = None) -> None:
    from core_config import sb_patch
    from core_embeddings import _embed_text_safe

    text_fn = _TEXT_FN.get(table)
    if not text_fn:
        return
    rid = str(row_id or row.get("id") or "").strip()
    if not rid:
        return
    text = text_fn(row).strip()
    if not text or len(text) < 5:
        return
    vec = _embed_text_safe(text)
    if not vec:
        return
    sb_patch(table, f"id=eq.{rid}", {"embedding": vec})
    print(f"[EMBED_SYNC] {table}:{rid} embedded ({len(vec)} dims)")


def _find_row_id(table: str, row: dict) -> str:
    from core_config import sb_get

    qs = _ID_QUERY.get(table, "select=id&order=id.desc&limit=1")
    if table == "knowledge_base" and row.get("topic") and row.get("domain"):
        qs = (
            "select=id"
            f"&domain=eq.{_sb_eq_value(row.get('domain'), 120)}"
            f"&topic=eq.{_sb_eq_value(row.get('topic'), 200)}"
            "&order=id.desc&limit=1"
        )
    elif table == "mistakes" and row.get("what_failed"):
        needle = _sb_eq_value(f"*{str(row.get('what_failed', ''))[:20]}*")
        qs = f"select=id&what_failed=ilike.{needle}&order=id.desc&limit=1"
    elif table == "behavioral_rules" and row.get("trigger"):
        qs = f"select=id&trigger=eq.{_sb_eq_value(row.get('trigger'), 120)}&order=id.desc&limit=1"
    elif table == "hot_reflections" and row.get("task_summary"):
        needle = _sb_eq_value(f"*{str(row.get('task_summary', ''))[:20]}*")
        qs = f"select=id&task_summary=ilike.{needle}&order=id.desc&limit=1"
    elif table == "conversation_episodes" and row.get("chat_id") and row.get("summary"):
        qs = (
            "select=id"
            f"&chat_id=eq.{_sb_eq_value(row.get('chat_id'), 120)}"
            f"&summary=ilike.{_sb_eq_value('*' + str(row.get('summary', ''))[:20] + '*')}"
            "&order=id.desc&limit=1"
        )
    rows = sb_get(table, qs, svc=True) or []
    if not rows:
        return ""
    return str(rows[0].get("id") or "").strip()

def _embed_new_row(table: str, row: dict) -> None:
    """
    Background thread: get new row id + embed it.
    Fires after sb_post() succeeds. Never raises.
    """
    try:
        rid = _find_row_id(table, row)
        if not rid:
            return
        _embed_row(table, row, rid)
    except Exception as e:
        print(f"[EMBED_SYNC] {table} auto-embed failed (non-fatal): {e}")


def _fire_embed_row_async(table: str, row: dict, row_id: str) -> None:
    if not row_id:
        return
    threading.Thread(target=_embed_row, args=(table, row, row_id), daemon=True).start()


def _refetch_and_embed_by_id_async(table: str, row_id: str) -> None:
    if not row_id:
        return

    def _task():
        try:
            from core_config import sb_get
            rows = sb_get(table, f"select={_select_for_table(table)}&id=eq.{row_id}&limit=1", svc=True) or []
            if rows:
                _embed_row(table, rows[0], row_id)
        except Exception as e:
            print(f"[EMBED_SYNC] {table}:{row_id} refetch embed failed (non-fatal): {e}")

    threading.Thread(target=_task, daemon=True).start()


def _fire_embed_async(table: str, row: dict) -> None:
    """Launch embed in background thread — never blocks the caller."""
    t = threading.Thread(target=_embed_new_row, args=(table, row), daemon=True)
    t.start()


def _fire_embed_for_id_async(table: str, row_id: str) -> None:
    if not row_id:
        return

    def _task():
        try:
            from core_config import sb_get
            rows = sb_get(table, f"select={_select_for_table(table)}&id=eq.{row_id}&limit=1", svc=True) or []
            if rows:
                _embed_row(table, rows[0], row_id)
        except Exception as e:
            print(f"[EMBED_SYNC] {table}:{row_id} refetch embed failed (non-fatal): {e}")

    threading.Thread(target=_task, daemon=True).start()

# ── Monkey-patch sb_post ───────────────────────────────────────────────────────

_PATCHED = False

def install():
    """
    Call once at startup (core_main.py on_start).
    Wraps core_config.sb_post with auto-embed logic.
    Safe to call multiple times — only patches once.
    """
    global _PATCHED
    if _PATCHED:
        return

    import core_config as _cc
    originals = {
        "sb_post": _cc.sb_post,
        "sb_upsert": getattr(_cc, "sb_upsert", None),
        "sb_patch": getattr(_cc, "sb_patch", None),
    }

    def sb_post_with_embed(table: str, data: dict, *args, **kwargs):
        result = originals["sb_post"](table, data, *args, **kwargs)
        if result and table in _TEXT_FN:
            row_id = str((data or {}).get("id") or "").strip()
            if row_id:
                _fire_embed_row_async(table, data, row_id)
            else:
                _fire_embed_async(table, data)
        return result

    def sb_upsert_with_embed(table: str, data: dict, on_conflict: str, *args, **kwargs):
        result = originals["sb_upsert"](table, data, on_conflict, *args, **kwargs)
        if result and table in _TEXT_FN:
            row_id = str((data or {}).get("id") or "").strip()
            if row_id:
                _fire_embed_row_async(table, data, row_id)
            else:
                _fire_embed_async(table, data)
        return result

    def sb_patch_with_embed(table: str, filters: str, data: dict, *args, **kwargs):
        result = originals["sb_patch"](table, filters, data, *args, **kwargs)
        if result and _relevant_patch(table, data):
            row_id = _parse_id_from_filters(filters)
            if row_id:
                _refetch_and_embed_by_id_async(table, row_id)
        return result

    _cc.sb_post = sb_post_with_embed
    if originals["sb_upsert"]:
        _cc.sb_upsert = sb_upsert_with_embed
    if originals["sb_patch"]:
        _cc.sb_patch = sb_patch_with_embed

    _rebind_module_globals("sb_post", sb_post_with_embed)
    if originals["sb_upsert"]:
        _rebind_module_globals("sb_upsert", sb_upsert_with_embed)
    if originals["sb_patch"]:
        _rebind_module_globals("sb_patch", sb_patch_with_embed)

    _PATCHED = True
    print("[EMBED_SYNC] Installed — auto-embedding on semantic writes and semantic patches")


def uninstall():
    """Restore original sb_post (for testing)."""
    global _PATCHED
    if not _PATCHED:
        return
    import core_config as _cc
    # Unwrap
    if hasattr(_cc.sb_post, '__wrapped__'):
        _cc.sb_post = _cc.sb_post.__wrapped__
    _PATCHED = False
    print("[EMBED_SYNC] Uninstalled")


# ── Also handle sb_post_critical (used in core_train for evolution_queue) ──────

def install_critical():
    """Also patch sb_post_critical if it exists in core_config."""
    try:
        import core_config as _cc
        if not hasattr(_cc, 'sb_post_critical'):
            return

        _original_critical = _cc.sb_post_critical

        def sb_post_critical_with_embed(table: str, data: dict, *args, **kwargs):
            result = _original_critical(table, data, *args, **kwargs)
            if result and table in _TEXT_FN:
                row_id = str((data or {}).get("id") or "").strip()
                if row_id:
                    _fire_embed_row_async(table, data, row_id)
                else:
                    _fire_embed_async(table, data)
            return result

        _cc.sb_post_critical = sb_post_critical_with_embed
        _rebind_module_globals("sb_post_critical", sb_post_critical_with_embed)
        print("[EMBED_SYNC] sb_post_critical also patched")
    except Exception as e:
        print(f"[EMBED_SYNC] sb_post_critical patch failed (non-fatal): {e}")
