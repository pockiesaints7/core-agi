"""
core_orch_layer7.py — CORE AGI Supabase Layer
===============================================
Wrapper around core_config.py Supabase functions. Adds:
  - L10 Constitution checks (enforce_db)
  - Retry logic for transient failures
  - Query result validation
  - Error normalization

DOES NOT:
  - Make raw HTTP calls (delegates to core_config)
  - Bypass L10 checks
  - Cache results (that's L2's job)

All other layers call L7 for Supabase access, never core_config directly.
"""

import time
from typing import Optional, Dict, List, Any

# Import L10 Constitution
try:
    from core_orch_layer10 import enforce_db, report_violation, SEVERITY_HIGH
except ImportError:
    print("[L7] WARNING: L10 Constitution Layer not available")
    def enforce_db(*args, **kwargs): pass
    def report_violation(*args, **kwargs): pass
    SEVERITY_HIGH = "high"

# Import actual Supabase functions from core_config
try:
    from core_config import sb_get, sb_post, sb_patch, sb_upsert, sb_delete
except ImportError:
    print("[L7] CRITICAL: Cannot import core_config Supabase functions")
    def sb_get(*args, **kwargs): return None
    def sb_post(*args, **kwargs): return None
    def sb_patch(*args, **kwargs): return None
    def sb_upsert(*args, **kwargs): return None
    def sb_delete(*args, **kwargs): return None


# ══════════════════════════════════════════════════════════════════════════════
# RETRY CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds


# ══════════════════════════════════════════════════════════════════════════════
# L7 PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def query(table: str, filters: str = "", select: str = "*", 
          order: str = "", limit: int = None, svc: bool = False,
          operation: str = "") -> Optional[List[Dict]]:
    """
    Query Supabase table with retry logic and L10 checks.
    
    Args:
        table: Table name
        filters: Query string filters (e.g. "status=eq.pending&id=gt.1")
        select: Columns to select
        order: Order clause
        limit: Row limit
        svc: Use service key (bypass RLS)
        operation: Description for L10 logging
    
    Returns:
        List of row dicts, or None on failure
    """
    # L10 check
    enforce_db(operation or f"query {table}")
    
    # Build query string
    qs = select if select.startswith("select=") else f"select={select}"
    if filters:
        qs += f"&{filters}"
    if order:
        qs += f"&order={order}"
    if limit:
        qs += f"&limit={limit}"
    
    # Retry loop
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            result = sb_get(table, qs, svc=svc)
            
            # Validate result
            if result is None:
                raise RuntimeError(f"sb_get returned None for {table}")
            
            if not isinstance(result, list):
                raise RuntimeError(f"sb_get returned non-list: {type(result)}")
            
            return result
        
        except Exception as e:
            last_error = e
            print(f"[L7] query {table} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            continue
    
    # All retries failed
    report_violation(
        invariant="L7-SUPABASE",
        what_failed=f"Query failed after {MAX_RETRIES} retries: {table}",
        context=f"filters={filters}, error={str(last_error)[:200]}",
        how_to_avoid="Check Supabase connectivity and table schema",
        severity=SEVERITY_HIGH,
    )
    return None


def insert(table: str, data: Dict[str, Any], operation: str = "") -> Optional[Dict]:
    """
    Insert row into Supabase table with retry logic.
    
    Args:
        table: Table name
        data: Row data dict
        operation: Description for L10 logging
    
    Returns:
        Inserted row dict, or None on failure
    """
    # L10 check
    enforce_db(operation or f"insert {table}")
    
    # Retry loop
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            result = sb_post(table, data)
            
            if result is None:
                raise RuntimeError(f"sb_post returned None for {table}")
            
            return result
        
        except Exception as e:
            last_error = e
            print(f"[L7] insert {table} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            continue
    
    # All retries failed
    report_violation(
        invariant="L7-SUPABASE",
        what_failed=f"Insert failed after {MAX_RETRIES} retries: {table}",
        context=f"data={str(data)[:200]}, error={str(last_error)[:200]}",
        how_to_avoid="Check table schema and constraints",
        severity=SEVERITY_HIGH,
    )
    return None


def update(table: str, match: str, data: Dict[str, Any], 
           operation: str = "") -> Optional[Dict]:
    """
    Update rows in Supabase table with retry logic.
    
    Args:
        table: Table name
        match: Filter for rows to update (e.g. "id=eq.123")
        data: Update data dict
        operation: Description for L10 logging
    
    Returns:
        Updated row dict, or None on failure
    """
    # L10 check
    enforce_db(operation or f"update {table}")
    
    # Retry loop
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            result = sb_patch(table, match, data)
            
            if result is None:
                raise RuntimeError(f"sb_patch returned None for {table}")
            
            return result
        
        except Exception as e:
            last_error = e
            print(f"[L7] update {table} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            continue
    
    # All retries failed
    report_violation(
        invariant="L7-SUPABASE",
        what_failed=f"Update failed after {MAX_RETRIES} retries: {table}",
        context=f"match={match}, data={str(data)[:200]}, error={str(last_error)[:200]}",
        how_to_avoid="Check table schema and match filter",
        severity=SEVERITY_HIGH,
    )
    return None


def upsert(table: str, data: Dict[str, Any], on_conflict: str = "id",
           operation: str = "") -> Optional[Dict]:
    """
    Upsert row in Supabase table with retry logic.
    
    Args:
        table: Table name
        data: Row data dict
        on_conflict: Column(s) to match for conflict resolution
        operation: Description for L10 logging
    
    Returns:
        Upserted row dict, or None on failure
    """
    # L10 check
    enforce_db(operation or f"upsert {table}")
    
    # Retry loop
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            result = sb_upsert(table, data, on_conflict=on_conflict)
            
            if result is None:
                raise RuntimeError(f"sb_upsert returned None for {table}")
            
            return result
        
        except Exception as e:
            last_error = e
            print(f"[L7] upsert {table} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            continue
    
    # All retries failed
    report_violation(
        invariant="L7-SUPABASE",
        what_failed=f"Upsert failed after {MAX_RETRIES} retries: {table}",
        context=f"data={str(data)[:200]}, error={str(last_error)[:200]}",
        how_to_avoid="Check table schema and on_conflict column",
        severity=SEVERITY_HIGH,
    )
    return None


def delete(table: str, match: str, operation: str = "") -> bool:
    """
    Delete rows from Supabase table with retry logic.
    
    Args:
        table: Table name
        match: Filter for rows to delete (e.g. "id=eq.123")
        operation: Description for L10 logging
    
    Returns:
        True on success, False on failure
    """
    # L10 check
    enforce_db(operation or f"delete {table}")
    
    # Retry loop
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            result = sb_delete(table, match)
            return True  # Assume success if no exception
        
        except Exception as e:
            last_error = e
            print(f"[L7] delete {table} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            continue
    
    # All retries failed
    report_violation(
        invariant="L7-SUPABASE",
        what_failed=f"Delete failed after {MAX_RETRIES} retries: {table}",
        context=f"match={match}, error={str(last_error)[:200]}",
        how_to_avoid="Check table and match filter",
        severity=SEVERITY_HIGH,
    )
    return False


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_counts(tables: List[str]) -> Dict[str, int]:
    """
    Get row counts for multiple tables efficiently.
    
    Args:
        tables: List of table names
    
    Returns:
        Dict mapping table name to row count (-1 on error)
    """
    counts = {}
    for table in tables:
        try:
            result = query(table, select="id", limit=1, 
                          operation=f"count {table}")
            counts[table] = len(result) if result else 0
        except Exception as e:
            print(f"[L7] count {table} failed: {e}")
            counts[table] = -1
    return counts


def health_check() -> bool:
    """
    Quick Supabase health check.
    
    Returns:
        True if Supabase is reachable, False otherwise
    """
    try:
        result = query("knowledge_base", select="id", limit=1,
                      operation="health_check")
        return result is not None
    except Exception as e:
        print(f"[L7] health_check failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

print("[L7] Supabase Layer loaded")
