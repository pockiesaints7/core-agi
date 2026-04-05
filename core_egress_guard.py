"""
core_egress_guard.py — Supabase Egress Rate Guard
==================================================
Wraps sb_get / sb_post / sb_patch / sb_upsert / sb_count with:
  1. Global token-bucket: max GLOBAL_MAX_CALLS_PER_HOUR calls/hour total
  2. Per-table hourly cap: max TABLE_MAX_CALLS_PER_HOUR calls/hour per table
  3. Dedup cache: identical (table, qs) GET responses cached for CACHE_TTL_S seconds
  4. Hard kill-switch: EGRESS_GUARD_DISABLED=1 bypasses everything

Env overrides:
  EGRESS_GUARD_DISABLED=1          bypass all limits
  EGRESS_GLOBAL_MAX_PER_HOUR=N     default 600
  EGRESS_TABLE_MAX_PER_HOUR=N      default 120
  EGRESS_CACHE_TTL_S=N             default 120

Usage: installed automatically at startup via core_main.py
"""

import os
import time
import threading
import hashlib
from collections import defaultdict, deque

# ── Config ────────────────────────────────────────────────────────────────────
DISABLED    = os.getenv("EGRESS_GUARD_DISABLED", "0").strip() in {"1", "true", "yes"}
GLOBAL_MAX  = int(os.getenv("EGRESS_GLOBAL_MAX_PER_HOUR", "600"))
TABLE_MAX   = int(os.getenv("EGRESS_TABLE_MAX_PER_HOUR", "120"))
CACHE_TTL_S = int(os.getenv("EGRESS_CACHE_TTL_S", "120"))

# High-churn write tables — stricter cap, never cache
HIGH_CHURN = {
    "market_snapshots", "hot_reflections", "sessions",
    "reasoning_log", "quality_metrics", "agentic_sessions",
    "tool_stats", "conversation_log",
}

# ── Internal state ─────────────────────────────────────────────────────────────
_lock         = threading.Lock()
_global_times = deque()
_table_times  = defaultdict(deque)
_get_cache    = {}
_stats        = defaultdict(int)
_installed    = False

# ── Helpers ───────────────────────────────────────────────────────────────────
def _trim(dq, window=3600.0):
    now = time.time()
    while dq and (now - dq[0]) > window:
        dq.popleft()

def _allow(table):
    if DISABLED:
        return True
    now = time.time()
    with _lock:
        _trim(_global_times)
        _trim(_table_times[table])
        if len(_global_times) >= GLOBAL_MAX:
            _stats["global_throttled"] += 1
            return False
        cap = max(20, TABLE_MAX // 2) if table in HIGH_CHURN else TABLE_MAX
        if len(_table_times[table]) >= cap:
            _stats[f"throttled_{table}"] += 1
            return False
        _global_times.append(now)
        _table_times[table].append(now)
        _stats[f"ok_{table}"] += 1
        return True

def _cache_key(table, qs):
    return hashlib.md5(f"{table}|{qs}".encode()).hexdigest()

def _cache_get(table, qs):
    if table in HIGH_CHURN:
        return None
    key = _cache_key(table, qs)
    with _lock:
        entry = _get_cache.get(key)
        if entry:
            result, exp = entry
            if time.time() < exp:
                _stats["cache_hit"] += 1
                return result
            del _get_cache[key]
    return None

def _cache_set(table, qs, result):
    if table in HIGH_CHURN:
        return
    key = _cache_key(table, qs)
    with _lock:
        _get_cache[key] = (result, time.time() + CACHE_TTL_S)

def _cache_invalidate():
    with _lock:
        _get_cache.clear()

def egress_stats():
    with _lock:
        _trim(_global_times)
        return {
            "global_calls_last_hour": len(_global_times),
            "global_max_per_hour": GLOBAL_MAX,
            "table_max_per_hour": TABLE_MAX,
            "cache_entries": len(_get_cache),
            "cache_ttl_s": CACHE_TTL_S,
            "disabled": DISABLED,
            **dict(_stats),
        }

# ── Patched wrappers ──────────────────────────────────────────────────────────
def _guarded_get(orig):
    def fn(table, qs="", svc=False, **kw):
        cached = _cache_get(table, qs)
        if cached is not None:
            return cached
        if not _allow(table):
            print(f"[EGRESS_GUARD] GET throttled: {table} -> []")
            return []
        result = orig(table, qs, svc=svc, **kw)
        _cache_set(table, qs, result)
        return result
    return fn

def _guarded_post(orig):
    def fn(table, row, **kw):
        if not _allow(table):
            print(f"[EGRESS_GUARD] POST throttled: {table} -> skipped")
            return None
        _cache_invalidate()
        return orig(table, row, **kw)
    return fn

def _guarded_patch(orig):
    def fn(table, qs, patch, **kw):
        if not _allow(table):
            print(f"[EGRESS_GUARD] PATCH throttled: {table} -> skipped")
            return None
        _cache_invalidate()
        return orig(table, qs, patch, **kw)
    return fn

def _guarded_upsert(orig):
    def fn(table, row, **kw):
        if not _allow(table):
            print(f"[EGRESS_GUARD] UPSERT throttled: {table} -> skipped")
            return None
        _cache_invalidate()
        return orig(table, row, **kw)
    return fn

def _guarded_count(orig):
    def fn(table, qs="", **kw):
        cached = _cache_get(table, f"__count__{qs}")
        if cached is not None:
            return cached
        if not _allow(table):
            print(f"[EGRESS_GUARD] COUNT throttled: {table} -> 0")
            return 0
        result = orig(table, qs, **kw)
        _cache_set(table, f"__count__{qs}", result)
        return result
    return fn

# ── Install ───────────────────────────────────────────────────────────────────
def install():
    global _installed
    if _installed:
        return
    if DISABLED:
        print("[EGRESS_GUARD] DISABLED via env — passthrough mode")
        _installed = True
        return
    try:
        import core_config as _cc
        if hasattr(_cc, "sb_get"):
            _cc.sb_get = _guarded_get(_cc.sb_get)
        if hasattr(_cc, "sb_post"):
            _cc.sb_post = _guarded_post(_cc.sb_post)
        if hasattr(_cc, "sb_patch"):
            _cc.sb_patch = _guarded_patch(_cc.sb_patch)
        if hasattr(_cc, "sb_upsert"):
            _cc.sb_upsert = _guarded_upsert(_cc.sb_upsert)
        if hasattr(_cc, "sb_count"):
            _cc.sb_count = _guarded_count(_cc.sb_count)
        _installed = True
        print(
            f"[EGRESS_GUARD] Installed — global {GLOBAL_MAX}/hr, "
            f"table {TABLE_MAX}/hr, cache {CACHE_TTL_S}s"
        )
    except Exception as e:
        print(f"[EGRESS_GUARD] install failed (non-fatal): {e}")
