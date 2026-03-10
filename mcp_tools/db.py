import os
import httpx
from typing import Optional
import hashlib, time

SUPABASE_REF = os.environ.get("SUPABASE_REF", "qbfaplqiakwjvrtwpbmr")
SUPABASE_PAT = os.environ.get("SUPABASE_PAT")
SUPABASE_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"

async def sql(query: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            SUPABASE_API,
            headers={"Authorization": f"Bearer {SUPABASE_PAT}", "Content-Type": "application/json"},
            json={"query": query}
        )
        r.raise_for_status()
        return r.json()

def esc(s: str) -> str:
    """Escape single quotes for SQL strings (used inside dollar-quoted strings)."""
    return str(s).replace("'", "''") if s else ""

def dq(s: str) -> str:
    """
    Safe dollar-quoting for arbitrary content.
    Uses a unique tag so content containing $$ won't break the quoting.
    Returns the full dollar-quoted literal: $tag$content$tag$
    """
    s = str(s) if s else ""
    # Generate a tag that cannot appear in the content
    tag = f"j{hashlib.md5((s + str(time.time())).encode()).hexdigest()[:6]}"
    # Fallback: keep regenerating if tag appears in content (extremely rare)
    while f"${tag}$" in s:
        tag = f"j{hashlib.md5((s + tag).encode()).hexdigest()[:6]}"
    return f"${tag}${s}${tag}$"
