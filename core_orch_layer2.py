"""
core_orch_layer2.py — CORE AGI Context Layer
=============================================
Gathers all context needed for reasoning:
  - Knowledge Base entries
  - Active tasks
  - Recent mistakes
  - Session history
  - Behavioral rules
  - Pattern frequency

Returns structured context dict that L3 uses for prompt construction.

DOES NOT:
  - Make reasoning decisions (that's L3)
  - Execute tools (that's L4)
  - Access Supabase directly (uses L7)
"""

from typing import Dict, List, Any, Optional
from datetime import datetime

# Import L7 for Supabase access
try:
    from core_orch_layer7 import query as sb_query
except ImportError:
    print("[L2] WARNING: L7 not available")
    def sb_query(*args, **kwargs): return None


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT GATHERING
# ══════════════════════════════════════════════════════════════════════════════

def gather_context(text: str, chat_id: str) -> Dict[str, Any]:
    """
    Gather all relevant context for a user message.
    
    Args:
        text: User message text
        chat_id: Telegram chat ID
    
    Returns:
        Context dict with keys:
        - kb_entries: List of relevant KB entries
        - tasks: List of active tasks
        - mistakes: List of recent mistakes
        - patterns: List of top patterns
        - rules: List of behavioral rules
        - session_state: Current session info
        - items: Total context items count
    """
    print(f"[L2] Gathering context for: {text[:50]}")
    
    context = {
        "kb_entries": [],
        "tasks": [],
        "mistakes": [],
        "patterns": [],
        "rules": [],
        "session_state": {},
        "items": 0,
    }
    
    # Extract keywords from message for targeted queries
    keywords = _extract_keywords(text)
    
    # Gather KB entries
    kb = _get_kb_context(keywords, text)
    if kb:
        context["kb_entries"] = kb
        context["items"] += len(kb)
    
    # Gather active tasks
    tasks = _get_task_context()
    if tasks:
        context["tasks"] = tasks
        context["items"] += len(tasks)
    
    # Gather recent mistakes
    mistakes = _get_mistake_context(keywords)
    if mistakes:
        context["mistakes"] = mistakes
        context["items"] += len(mistakes)
    
    # Gather patterns
    patterns = _get_pattern_context(keywords)
    if patterns:
        context["patterns"] = patterns
        context["items"] += len(patterns)
    
    # Gather behavioral rules
    rules = _get_rules_context()
    if rules:
        context["rules"] = rules
        context["items"] += len(rules)
    
    # Get session state
    session_state = _get_session_state(chat_id)
    if session_state:
        context["session_state"] = session_state
    
    print(f"[L2] Context gathered: {context['items']} items")
    return context


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text for targeted queries."""
    import re
    
    # Stop words to filter out
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "i", "you", "we", "it",
        "to", "of", "and", "or", "in", "on", "at", "for", "with", "do", "did",
        "can", "could", "would", "please", "what", "when", "where", "how", "why",
    }
    
    # Extract words (5+ chars, not stop words)
    words = re.findall(r'\b[a-zA-Z]{5,}\b', text.lower())
    keywords = [w for w in words if w not in stop_words]
    
    # Return unique keywords (max 5)
    return list(dict.fromkeys(keywords))[:5]


def _get_kb_context(keywords: List[str], full_text: str) -> List[Dict]:
    """Get relevant KB entries based on keywords or full text search."""
    try:
        if not keywords:
            # No keywords - get recent high-confidence entries
            result = sb_query(
                "knowledge_base",
                filters="active=eq.true&id=gt.1",
                select="id,domain,topic,content,confidence",
                order="confidence.desc,created_at.desc",
                limit=5,
                operation="get_kb_context"
            )
            return result or []
        
        # Keyword-based search
        kw = keywords[0]
        result = sb_query(
            "knowledge_base",
            filters=f"topic.ilike.*{kw}*&active=eq.true&id=gt.1",
            select="id,domain,topic,content,confidence",
            order="confidence.desc",
            limit=5,
            operation="get_kb_context"
        )
        
        if result:
            return result
        
        # Fallback to recent entries if no matches
        result = sb_query(
            "knowledge_base",
            filters="active=eq.true&id=gt.1",
            select="id,domain,topic,content,confidence",
            order="created_at.desc",
            limit=3,
            operation="get_kb_context"
        )
        return result or []
    
    except Exception as e:
        print(f"[L2] _get_kb_context failed: {e}")
        return []


def _get_task_context() -> List[Dict]:
    """Get active and pending tasks."""
    try:
        # Get in_progress tasks first
        in_progress = sb_query(
            "task_queue",
            filters="status=eq.in_progress&id=gt.1",
            select="id,task,priority,status,domain",
            order="priority.desc",
            limit=3,
            operation="get_task_context"
        )
        
        if in_progress:
            return in_progress
        
        # No in_progress - get pending
        pending = sb_query(
            "task_queue",
            filters="status=eq.pending&id=gt.1",
            select="id,task,priority,status,domain",
            order="priority.desc",
            limit=3,
            operation="get_task_context"
        )
        return pending or []
    
    except Exception as e:
        print(f"[L2] _get_task_context failed: {e}")
        return []


def _get_mistake_context(keywords: List[str]) -> List[Dict]:
    """Get recent mistakes, optionally filtered by keywords."""
    try:
        if keywords:
            # Keyword-scoped search
            kw = keywords[0]
            result = sb_query(
                "mistakes",
                filters=f"id=gt.1&what_failed.ilike.*{kw}*",
                select="id,domain,what_failed,correct_approach,how_to_avoid,severity",
                order="created_at.desc",
                limit=4,
                operation="get_mistake_context"
            )
            
            if result:
                return result
        
        # No keywords or no matches - get recent high-severity
        result = sb_query(
            "mistakes",
            filters="id=gt.1",
            select="id,domain,what_failed,correct_approach,how_to_avoid,severity",
            order="created_at.desc",
            limit=4,
            operation="get_mistake_context"
        )
        return result or []
    
    except Exception as e:
        print(f"[L2] _get_mistake_context failed: {e}")
        return []


def _get_pattern_context(keywords: List[str]) -> List[Dict]:
    """Get top patterns, optionally filtered by keywords."""
    try:
        if keywords:
            # Keyword-scoped search
            kw = keywords[0]
            result = sb_query(
                "pattern_frequency",
                filters=f"id=gt.1&stale=eq.false&pattern_key.ilike.*{kw}*",
                select="id,pattern_key,frequency,domain,description",
                order="frequency.desc",
                limit=5,
                operation="get_pattern_context"
            )
            
            if result:
                return result
        
        # No keywords or no matches - get top by frequency
        result = sb_query(
            "pattern_frequency",
            filters="id=gt.1&stale=eq.false",
            select="id,pattern_key,frequency,domain,description",
            order="frequency.desc",
            limit=6,
            operation="get_pattern_context"
        )
        return result or []
    
    except Exception as e:
        print(f"[L2] _get_pattern_context failed: {e}")
        return []


def _get_rules_context() -> List[Dict]:
    """Get active behavioral rules."""
    try:
        result = sb_query(
            "behavioral_rules",
            filters="active=eq.true&id=gt.1",
            select="id,domain,trigger,pointer,confidence",
            order="confidence.desc,created_at.desc",
            limit=10,
            operation="get_rules_context"
        )
        return result or []
    
    except Exception as e:
        print(f"[L2] _get_rules_context failed: {e}")
        return []


def _get_session_state(chat_id: str) -> Dict[str, Any]:
    """Get current session state info."""
    try:
        # Get latest session
        sessions = sb_query(
            "sessions",
            filters="id=gt.1",
            select="id,summary,domain,quality_score,created_at",
            order="created_at.desc",
            limit=1,
            operation="get_session_state"
        )
        
        if sessions:
            return sessions[0]
        
        return {}
    
    except Exception as e:
        print(f"[L2] _get_session_state failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT FORMATTING
# ══════════════════════════════════════════════════════════════════════════════

def format_context_for_prompt(context: Dict[str, Any]) -> str:
    """
    Format context dict into readable string for LLM prompt.
    
    Args:
        context: Context dict from gather_context()
    
    Returns:
        Formatted string
    """
    sections = []
    
    # KB entries
    if context.get("kb_entries"):
        lines = []
        for entry in context["kb_entries"][:5]:
            topic = entry.get("topic", "")
            content = entry.get("content", "")[:200]
            domain = entry.get("domain", "")
            lines.append(f"  [{domain}] {topic}: {content}")
        sections.append("KNOWLEDGE BASE:\n" + "\n".join(lines))
    
    # Active tasks
    if context.get("tasks"):
        lines = []
        for task in context["tasks"][:3]:
            task_text = str(task.get("task", ""))[:100]
            priority = task.get("priority", "?")
            status = task.get("status", "?")
            lines.append(f"  [P{priority}/{status}] {task_text}")
        sections.append("ACTIVE TASKS:\n" + "\n".join(lines))
    
    # Recent mistakes
    if context.get("mistakes"):
        lines = []
        for mistake in context["mistakes"][:4]:
            what = mistake.get("what_failed", "")[:80]
            fix = mistake.get("correct_approach") or mistake.get("how_to_avoid", "")
            domain = mistake.get("domain", "?")
            lines.append(f"  [{domain}] AVOID: {what} → {fix[:80]}")
        sections.append("RECENT MISTAKES:\n" + "\n".join(lines))
    
    # Top patterns
    if context.get("patterns"):
        lines = []
        for pattern in context["patterns"][:5]:
            key = pattern.get("pattern_key", "")
            freq = pattern.get("frequency", 0)
            domain = pattern.get("domain", "")
            desc = pattern.get("description", "")[:100]
            lines.append(f"  [{domain}/{freq}x] {key}: {desc}")
        sections.append("TOP PATTERNS:\n" + "\n".join(lines))
    
    # Behavioral rules
    if context.get("rules"):
        lines = []
        for rule in context["rules"][:10]:
            trigger = rule.get("trigger", "")
            pointer = rule.get("pointer", "")[:80]
            lines.append(f"  [{trigger}] {pointer}")
        sections.append("BEHAVIORAL RULES:\n" + "\n".join(lines))
    
    if not sections:
        return ""
    
    return "\n\n".join(sections)


# ══════════════════════════════════════════════════════════════════════════════
# INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

print("[L2] Context Layer loaded")
