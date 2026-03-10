from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List
from modules.db import sql, esc

router = APIRouter(tags=["brain"])

class Memory(BaseModel):
    category: str
    key: str
    value: str

class Knowledge(BaseModel):
    domain: str
    topic: str
    content: str
    tags: List[str] = []
    confidence: str = "high"
    source: Optional[str] = None

class Mistake(BaseModel):
    context: str
    what_failed: str
    root_cause: Optional[str] = None
    correct_approach: Optional[str] = None
    tags: List[str] = []

class Playbook(BaseModel):
    topic: str
    method: str
    why_best: Optional[str] = None
    supersedes: Optional[str] = None
    tags: List[str] = []

class Session(BaseModel):
    summary: str
    actions: List[str] = []
    interface: Optional[str] = "claude-ai"

@router.get("/boot")
async def boot():
    results = {}
    tables = {
        "memory":         "SELECT * FROM memory ORDER BY category, key",
        "knowledge_base": "SELECT id, domain, topic, tags, confidence FROM knowledge_base ORDER BY id",
        "mistakes":       "SELECT id, context, what_failed, root_cause, correct_approach FROM mistakes ORDER BY id",
        "playbook":       "SELECT topic, method, why_best, supersedes, previous_method, version FROM playbook ORDER BY topic",
        "sessions":       "SELECT summary, actions, created_at FROM sessions ORDER BY created_at DESC LIMIT 20",
    }
    for name, query in tables.items():
        try:
            results[name] = await sql(query)
        except Exception as e:
            results[name] = {"error": str(e)}
    return results

@router.post("/memory")
async def save_memory(m: Memory):
    q = f"INSERT INTO memory (category, key, value) VALUES ($${esc(m.category)}$$, $${esc(m.key)}$$, $${esc(m.value)}$$) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
    return await sql(q)

@router.post("/knowledge")
async def save_knowledge(k: Knowledge):
    tags_sql = "{" + ",".join(k.tags) + "}"
    src = esc(k.source) if k.source else ""
    q = f"INSERT INTO knowledge_base (domain, topic, content, tags, confidence, source) VALUES ($${esc(k.domain)}$$, $${esc(k.topic)}$$, $${esc(k.content)}$$, '{tags_sql}', $${esc(k.confidence)}$$, $${src}$$) ON CONFLICT (topic) DO UPDATE SET content=EXCLUDED.content, tags=EXCLUDED.tags, confidence=EXCLUDED.confidence, updated_at=NOW()"
    return await sql(q)

@router.get("/knowledge/{topic}")
async def get_knowledge(topic: str):
    return await sql(f"SELECT * FROM knowledge_base WHERE topic = $${esc(topic)}$$")

@router.get("/search")
async def search(q: str):
    return await sql(f"SELECT id, domain, topic, content, tags FROM knowledge_base WHERE to_tsvector('english', topic || ' ' || content) @@ plainto_tsquery('english', $${esc(q)}$$) LIMIT 10")

@router.post("/mistake")
async def save_mistake(m: Mistake):
    tags_sql = "{" + ",".join(m.tags) + "}"
    rc = esc(m.root_cause) if m.root_cause else esc(m.what_failed)
    q = f"INSERT INTO mistakes (context, what_failed, root_cause, correct_approach, tags) VALUES ($${esc(m.context)}$$, $${esc(m.what_failed)}$$, $${rc}$$, $${esc(m.correct_approach)}$$, '{tags_sql}')"
    return await sql(q)

@router.post("/playbook")
async def save_playbook(p: Playbook):
    """
    Upsert a playbook entry with version history.
    On conflict: saves old method to previous_method, bumps version, updates method.
    Old method is NEVER deleted - always preserved in previous_method for reference.
    """
    tags_sql = "{" + ",".join(p.tags) + "}"
    sup = esc(p.supersedes) if p.supersedes else ""
    q = f"""
INSERT INTO playbook (topic, method, why_best, supersedes, tags, version, previous_method)
VALUES ($${esc(p.topic)}$$, $${esc(p.method)}$$, $${esc(p.why_best)}$$, $${sup}$$, '{tags_sql}', 1, NULL)
ON CONFLICT (topic) DO UPDATE SET
    previous_method = playbook.method,
    method          = EXCLUDED.method,
    why_best        = EXCLUDED.why_best,
    supersedes      = EXCLUDED.supersedes,
    tags            = EXCLUDED.tags,
    version         = playbook.version + 1,
    updated_at      = NOW()
"""
    return await sql(q)

@router.get("/playbook/{topic}/history")
async def playbook_history(topic: str):
    """Get current method + previous version for a topic."""
    return await sql(f"SELECT topic, method, previous_method, version, why_best, updated_at FROM playbook WHERE topic = $${esc(topic)}$$")

@router.post("/session")
async def log_session(s: Session):
    actions_sql = "{" + ",".join([f'"{esc(a)}"' for a in s.actions]) + "}"
    q = f"INSERT INTO sessions (summary, actions) VALUES ($${esc(s.summary)}$$, '{actions_sql}')"
    return await sql(q)
