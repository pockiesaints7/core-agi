import os, json, httpx, anthropic
from supabase import create_client
from datetime import datetime

ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHATID = os.environ["TELEGRAM_CHAT_ID"]
JARVIS_SECRET   = os.environ["JARVIS_SECRET"]
JARVIS_URL      = os.environ.get("JARVIS_URL", "https://jarvis-os-production.up.railway.app")
MODEL           = "claude-sonnet-4-5"

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

def notify(msg):
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHATID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")

def db_get(table, filters={}):
    try:
        q = db.table(table).select("*")
        for k,v in filters.items():
            q = q.eq(k, v)
        return q.execute().data
    except:
        return []

def db_put(table, data):
    try:
        db.table(table).insert(data).execute()
    except Exception as e:
        print(f"[DB ERROR] {e}")

def call_agent(system, user, max_tokens=2000):
    try:
        msg = ai.messages.create(model=MODEL, max_tokens=max_tokens,
            system=system, messages=[{"role":"user","content":user}])
        return msg.content[0].text
    except Exception as e:
        return f"[AGENT ERROR] {e}"

MASTER = """You are CORE, a self-improving universal execution system.
Execute missions. Never guess. Verify then act. Learn every cycle.

PHASE 0: Identify domain, complexity, output type, unknowns.
PHASE 1: Use patterns/mistakes/knowledge blocks from context.
PHASE 2: Design agent sequence for this task.
PHASE 3: Simulate 5 failure points with mitigations.
PHASE 4: Execute fully. Never skip. Unknown means say UNKNOWN.
PHASE 5: Score 0-100. Below 85 improve weakest part. Max 3 loops.
PHASE 6: Output result then end with JSON block.

End every response with:
{"domain":"...","task_type":"...","quality_score":0,"agents_used":[],"key_learning":"...","mistake_to_avoid":"..."}
"""

def run(task):
    start = datetime.utcnow()
    print(f"\n[CORE] Task: {task}")
    domain = call_agent("One word only: the domain of this task. E.g. software, finance, construction, legal", task).strip().lower().split()[0]
    patterns = db_get("patterns", {"domain": domain})
    mistakes  = db_get("mistakes",  {"domain": domain})
    blocks    = db_get("knowledge_blocks", {"domain": domain})
    context = f"TASK: {task}\n\nPATTERNS: {json.dumps(patterns[:3])}\nMISTAKES: {json.dumps(mistakes[:3])}\nBLOCKS: {json.dumps(blocks[:3])}"
    result = call_agent(MASTER, context, max_tokens=4000)
    try:
        js = result[result.rfind("{"):result.rfind("}")+1]
        L  = json.loads(js)
        db_put("patterns", {"domain": L.get("domain",domain), "task_type": L.get("task_type","general"),
            "agent_sequence": str(L.get("agents_used",[])), "quality_score": L.get("quality_score",0),
            "notes": L.get("key_learning",""), "created_at": start.isoformat()})
        if L.get("mistake_to_avoid"):
            db_put("mistakes", {"domain": L.get("domain",domain), "what_failed": L.get("mistake_to_avoid",""),
                "why_it_failed":"logged by CORE","how_to_avoid":L.get("key_learning",""),
                "severity":"medium","created_at":start.isoformat()})
        db_put("session_learning", {"task":task,"domain":domain,"quality_score":L.get("quality_score",0),
            "key_learning":L.get("key_learning",""),"created_at":start.isoformat()})
        notify(f"CORE: Task complete\nDomain: {domain}\nQuality: {L.get('quality_score',0)}/100")
    except Exception as e:
        print(f"[STORE ERROR] {e}")
        notify(f"CORE: Storage error - {e}")
    print(f"[CORE] Done in {(datetime.utcnow()-start).total_seconds():.1f}s")
    return result

if __name__ == "__main__":
    import sys
    output = run(" ".join(sys.argv[1:]) if len(sys.argv)>1 else "CORE self-test: verify all systems operational")
    print("\n" + "="*60 + "\n" + output)
