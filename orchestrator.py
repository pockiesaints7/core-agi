import os, json, httpx, time, threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT           = os.environ.get("GITHUB_PAT", "")
GITHUB_USERNAME      = os.environ.get("GITHUB_USERNAME", "")
PORT                 = int(os.environ.get("PORT", 8080))

SB = {
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "apikey": SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# ── TELEGRAM ─────────────────────────────────────────────
def notify(msg, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                   data={"chat_id": cid, "text": msg[:4000], "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN","")
    if not domain: return
    r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                   data={"url": f"https://{domain}/webhook"})
    print(f"[CORE] Webhook: {r.json().get('description','')}")

# ── SUPABASE CORE READS ───────────────────────────────────
def sb_get(endpoint, params=""):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{endpoint}{params}", headers=SB)
    return r.json()

def sb_post(endpoint, data):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=SB, json=data)
    return r.json()

def sb_patch(endpoint, data):
    httpx.patch(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=SB, json=data)

# ── LOAD MASTER PROMPT ────────────────────────────────────
def load_master_prompt():
    data = sb_get("master_prompt", "?is_active=eq.true&order=version.desc&limit=1")
    if data:
        sb_patch(f"master_prompt?is_active=eq.true", {"last_used_at": datetime.utcnow().isoformat()})
        print(f"[CORE] Loaded master_prompt v{data[0]['version']}")
        return data[0]["content"], data[0]["version"]
    return "", 0

# ── RICH CONTEXT FROM JARVIS-BRAIN ───────────────────────
def get_context(domain, task_keywords=""):
    """Pull relevant knowledge from ALL jarvis-brain tables via agi_context view"""
    # Search by domain
    domain_ctx = sb_get("agi_context", f"?domain=eq.{domain}&limit=10")
    # Search by keyword in key field
    kw_ctx = []
    if task_keywords:
        word = task_keywords.split()[0] if task_keywords else ""
        kw_ctx = sb_get("agi_context", f"?key=ilike.*{word}*&limit=5")
    combined = domain_ctx + [x for x in kw_ctx if x not in domain_ctx]
    return combined[:12]

def get_mistakes(domain):
    """Pull from unified agi_mistakes view covering all mistake sources"""
    domain_mistakes = sb_get("agi_mistakes", f"?domain=eq.{domain}&limit=10")
    general = sb_get("agi_mistakes", f"?domain=eq.general&limit=5")
    return domain_mistakes + general

def get_playbook(topic_keyword):
    """Get best method for a topic directly from playbook"""
    if not topic_keyword: return []
    word = topic_keyword.split()[0]
    return sb_get("playbook", f"?topic=ilike.*{word}*&limit=3")

def get_agi_status():
    """System health from agi_status view"""
    data = sb_get("agi_status", "")
    return data[0] if data else {}

def get_memory(key):
    """Recall specific fact from memory table"""
    data = sb_get("memory", f"?key=eq.{key}&limit=1")
    return data[0]["value"] if data else None

# ── STORE LEARNINGS BACK ─────────────────────────────────
def store_pattern(domain, task_type, agents, score, services, notes):
    sb_post("patterns", {
        "domain": domain, "task_type": task_type, "agent_sequence": agents,
        "quality_score": score, "services_used": services, "notes": notes, "execution_time": 0
    })

def store_learning(summary, pattern, mistake, improvement):
    sb_post("session_learning", {
        "task_summary": summary, "new_pattern": pattern,
        "mistake_to_avoid": mistake, "estimated_improvement": improvement
    })

def store_knowledge(domain, topic, content, tags, confidence="learned"):
    """Add new knowledge back to knowledge_base so system grows smarter"""
    sb_post("knowledge_base", {
        "domain": domain, "topic": topic, "content": content,
        "source": "core_agi", "confidence": confidence, "tags": tags
    })

def store_mistake(context, what_failed, correct_approach, tags, domain="general"):
    """Add new mistake to prevent future repetition"""
    sb_post("mistakes", {
        "context": context, "what_failed": what_failed,
        "correct_approach": correct_approach, "how_to_avoid": correct_approach,
        "domain": domain, "tags": tags, "severity": "medium"
    })

def log_session(summary, actions, interface="telegram"):
    """Log session to sessions table"""
    sb_post("sessions", {
        "summary": summary, "actions": actions, "interface": interface
    })

# ── MASTER PROMPT EVOLUTION ───────────────────────────────
def update_master_prompt(content, reason, score=90):
    sb_patch("master_prompt?is_active=eq.true", {"is_active": False})
    data = sb_get("master_prompt", "?order=version.desc&limit=1")
    next_v = (data[0]["version"] + 1) if data else 3
    sb_post("master_prompt", {
        "version": next_v, "content": content,
        "change_reason": reason, "quality_score": score, "is_active": True
    })
    print(f"[CORE] Master prompt -> v{next_v}")
    return next_v

def sync_to_github(content, version):
    if not GITHUB_PAT: return
    import base64
    repo = f"{GITHUB_USERNAME}/core-agi"
    h = {"Authorization": f"Bearer {GITHUB_PAT}", "Content-Type": "application/json", "User-Agent": "CORE"}
    encoded = base64.b64encode(content.encode()).decode()
    sha = None
    try:
        sha = httpx.get(f"https://api.github.com/repos/{repo}/contents/master_prompt.md", headers=h).json().get("sha")
    except: pass
    payload = {"message": f"Auto-sync master_prompt v{version}", "content": encoded}
    if sha: payload["sha"] = sha
    httpx.put(f"https://api.github.com/repos/{repo}/contents/master_prompt.md", headers=h, json=payload)
    print(f"[CORE] GitHub synced v{version}")

# ── CLAUDE CALLS ─────────────────────────────────────────
ORCHESTRATOR_PROMPT = 'You are CORE orchestrator. Analyze the task and output ONLY valid JSON: {"domain":"software","task_type":"web_app","agents":[{"role":"researcher","task":"research X"},{"role":"engineer","task":"build Y"}],"services":["supabase"]}'
CRITIC_PROMPT = 'You are CORE critic. Score 0-100. Output ONLY valid JSON: {"score":85,"issues":[],"verdict":"approved","improvement":""}'
PROMPT_EVOLVER = 'You are CORE prompt engineer. Given current master prompt and completed task summary, return full improved prompt text OR exactly: NO_CHANGE. Be conservative - only improve if there is a clear gap in the current prompt.'
KNOWLEDGE_EXTRACTOR = 'You are a knowledge extractor. Given a completed task and its output, extract new reusable knowledge. Output ONLY valid JSON: {"new_knowledge":[{"domain":"X","topic":"Y","content":"Z","tags":["a","b"]}],"new_mistakes":[]}'

AGENT_PROMPTS = {
    "researcher": "You are a world-class researcher. Be comprehensive and specific. Always cite what you know vs what is uncertain.",
    "planner": "You are a master project planner. Create clear phases, milestones, dependencies, and risk mitigations.",
    "engineer": "You are a senior software engineer. Write clean, production-ready, well-commented code.",
    "designer": "You are a UI/UX designer. Create detailed design specifications, user flows, and component descriptions.",
    "writer": "You are a professional technical writer. Produce clear, well-structured, comprehensive documents.",
    "analyst": "You are a data analyst and estimator. Provide accurate calculations, assumptions, and breakdowns.",
    "qa": "You are a QA engineer. Review thoroughly for quality, completeness, edge cases, and potential failures."
}

def call_claude(system, user, context=""):
    content = f"{user}\n\nContext:\n{context}" if context else user
    r = httpx.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-5", "max_tokens": 4096, "system": system,
              "messages": [{"role": "user", "content": content}]}, timeout=90)
    data = r.json()
    return data["content"][0]["text"] if data.get("content") else ""

# ── MAIN EXECUTE ─────────────────────────────────────────
def execute_task(user_task, reply_chat_id=None):
    cid = reply_chat_id or TELEGRAM_CHAT_ID
    notify(f"CORE: Starting...\n`{user_task[:80]}`", cid)
    master_content, master_version = load_master_prompt()
    start = datetime.now()

    # Orchestrate
    plan_raw = call_claude(ORCHESTRATOR_PROMPT, user_task)
    try: plan = json.loads(plan_raw)
    except: plan = {"domain":"general","task_type":"unknown","agents":[{"role":"writer","task":user_task}],"services":[]}

    domain    = plan.get("domain","general")
    task_type = plan.get("task_type","unknown")
    agents    = plan.get("agents",[])

    # === JARVIS-BRAIN CONTEXT INJECTION ===
    # Pull from ALL rich tables - this is what makes it smarter than a basic agent
    knowledge   = get_context(domain, user_task)
    mistakes    = get_mistakes(domain)
    playbook    = get_playbook(domain)

    knowledge_text = "\n".join([f"- [{x['source_table']}] {x['key']}: {x.get('value','')[:200]}" for x in knowledge]) or "None"
    mistakes_text  = "\n".join([f"- AVOID: {m['what_failed'][:100]}" for m in mistakes]) or "None"
    playbook_text  = "\n".join([f"- METHOD: {p['topic']}: {p['method'][:150]}" for p in playbook]) or "None"

    context = f"""Task: {user_task}

JARVIS-BRAIN KNOWLEDGE ({len(knowledge)} entries):
{knowledge_text}

PROVEN METHODS (playbook):
{playbook_text}

MISTAKES TO AVOID ({len(mistakes)} entries):
{mistakes_text}"""

    results = {}

    # Run agents
    for agent_def in agents:
        role = agent_def.get("role","writer")
        task = agent_def.get("task", user_task)
        print(f"  -> {role}...")
        out = call_claude(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]), task, context)
        results[role] = out
        context += f"\n\n{role.upper()} OUTPUT:\n{out}"

    # Critic loop
    try:
        critic = json.loads(call_claude(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}"))
        score  = critic.get("score", 75)
    except: score, critic = 75, {"score":75,"issues":[]}

    attempts = 1
    while score < 85 and attempts < 3:
        role  = agents[-1]["role"] if agents else "writer"
        retry = call_claude(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]),
                            f"Fix these issues: {critic.get('issues',[])}. Task: {user_task}", context)
        context += f"\n\nIMPROVED:\n{retry}"
        try:
            critic = json.loads(call_claude(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}"))
            score  = critic.get("score", 75)
        except: score = 75
        attempts += 1

    # === STORE LEARNINGS BACK TO JARVIS-BRAIN ===
    store_pattern(domain, task_type, [a["role"] for a in agents], score, plan.get("services",[]), user_task[:100])
    store_learning(user_task[:200], str([a["role"] for a in agents]), mistakes_text[:200], min(score/100,1.0))
    log_session(f"Task: {user_task[:100]}", [a["role"] for a in agents])

    # Extract and store new knowledge
    try:
        extracted_raw = call_claude(KNOWLEDGE_EXTRACTOR, f"Task: {user_task}\n\nOutput:\n{context[:2000]}")
        extracted = json.loads(extracted_raw)
        for k in extracted.get("new_knowledge",[]):
            store_knowledge(k["domain"], k["topic"], k["content"], k.get("tags",[]), "learned")
        for m in extracted.get("new_mistakes",[]):
            store_mistake(user_task[:100], m.get("what",""), m.get("avoid",""), [domain])
        print(f"[CORE] Stored {len(extracted.get('new_knowledge',[]))} knowledge + {len(extracted.get('new_mistakes',[]))} mistakes")
    except Exception as e:
        print(f"[CORE] Knowledge extraction error: {e}")

    # Evolve master prompt if warranted
    evolution_input = f"Current master prompt:\n{master_content[:1000]}\n\nCompleted task: {user_task}\nScore: {score}\nKnowledge used: {len(knowledge)} entries"
    evolved = call_claude(PROMPT_EVOLVER, evolution_input)
    if evolved.strip() != "NO_CHANGE" and len(evolved) > 100:
        new_v = update_master_prompt(evolved, f"Auto-evolved: {user_task[:60]}", score)
        sync_to_github(evolved, new_v)
        notify(f"Master prompt evolved to v{new_v}", cid)

    duration = (datetime.now() - start).seconds
    status   = get_agi_status()
    first    = list(results.values())[0][:400] if results else ""

    notify(f"""CORE Task Done
Task: {user_task[:60]}
Score: {score}/100
Agents: {', '.join([a['role'] for a in agents])}
Time: {duration}s
Knowledge used: {len(knowledge)} entries
Mistakes avoided: {len(mistakes)}
DB: {status.get('knowledge_entries',0)} knowledge | {status.get('pattern_entries',0)} patterns

Preview:
{first}...""", cid)

    return context

# ── TELEGRAM COMMANDS ─────────────────────────────────────
def handle_message(message):
    chat_id = str(message.get("chat",{}).get("id",""))
    text    = message.get("text","").strip()
    if not text: return
    print(f"[CORE] {chat_id}: {text[:60]}")

    if text == "/start":
        s = get_agi_status()
        notify(f"""CORE AGI Online

Knowledge: {s.get('knowledge_entries',0)} entries
Playbook: {s.get('playbook_entries',0)} methods
Mistakes logged: {s.get('mistake_entries',0)}
Patterns learned: {s.get('pattern_entries',0)}
Master prompt: v{s.get('master_prompt_version','?')}

Commands:
/status - full system status
/prompt - current master prompt
/tasks - recent tasks
/ask [question] - query knowledge base

Or just send any task to execute it.""", chat_id)

    elif text == "/status":
        s = get_agi_status()
        notify(f"""CORE Status
Knowledge: {s.get('knowledge_entries',0)}
Playbook: {s.get('playbook_entries',0)}
Mistakes: {s.get('mistake_entries',0)}
Memory facts: {s.get('memory_entries',0)}
Patterns: {s.get('pattern_entries',0)}
Learnings: {s.get('learnings',0)}
Pending tasks: {s.get('pending_tasks',0)}
Done tasks: {s.get('completed_tasks',0)}
Master prompt: v{s.get('master_prompt_version','?')}
Last updated: {str(s.get('prompt_last_updated','?'))[:10]}""", chat_id)

    elif text == "/prompt":
        content, v = load_master_prompt()
        notify(f"Master Prompt v{v}\n\n{content[:800]}...", chat_id)

    elif text == "/tasks":
        tasks = sb_get("patterns", "?order=created_at.desc&limit=5")
        msg   = "Recent Tasks\n\n"
        msg  += "\n".join([f"- {t.get('notes','?')[:50]} (score:{t.get('quality_score',0)})" for t in tasks]) if tasks else "No tasks yet."
        notify(msg, chat_id)

    elif text.startswith("/ask "):
        query = text[5:].strip()
        word  = query.split()[0] if query else ""
        results = sb_get("agi_context", f"?key=ilike.*{word}*&limit=5")
        if results:
            msg = f"Knowledge on '{query}':\n\n"
            msg += "\n\n".join([f"[{r['source_table']}] {r['key']}:\n{str(r.get('value',''))[:200]}" for r in results])
        else:
            msg = f"No knowledge found for '{query}'"
        notify(msg, chat_id)

    else:
        threading.Thread(target=execute_task, args=(text, chat_id), daemon=True).start()

# ── WEBHOOK SERVER ────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook":
            body = self.rfile.read(int(self.headers.get("Content-Length",0)))
            self.send_response(200); self.end_headers()
            try:
                update = json.loads(body)
                if "message" in update:
                    handle_message(update["message"])
            except Exception as e:
                print(f"Webhook error: {e}")
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        self.send_response(200); self.end_headers()
        s = get_agi_status()
        self.wfile.write(f"CORE v3 | Prompt v{s.get('master_prompt_version','?')} | Knowledge: {s.get('knowledge_entries',0)} | Patterns: {s.get('pattern_entries',0)}".encode())

    def log_message(self, *args): pass

# ── QUEUE POLLER ─────────────────────────────────────────
def poll_queue():
    while True:
        try:
            tasks = sb_get("task_queue", "?status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                task = tasks[0]
                sb_patch(f"task_queue?id=eq.{task['id']}", {"status":"running"})
                try:
                    result = execute_task(task["task"], task.get("chat_id"))
                    sb_patch(f"task_queue?id=eq.{task['id']}", {"status":"done","result":result[:5000]})
                except Exception as e:
                    sb_patch(f"task_queue?id=eq.{task['id']}", {"status":"failed","error":str(e)})
                    notify(f"CORE queue failed: {str(e)[:100]}")
        except Exception as e:
            print(f"[CORE] Poll error: {e}")
        time.sleep(30)

# ── MAIN ─────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[CORE] Starting on port {PORT}")
    set_webhook()
    s = get_agi_status()
    notify(f"""CORE v3 Online
Knowledge: {s.get('knowledge_entries',0)} entries loaded
Playbook: {s.get('playbook_entries',0)} methods
Mistakes: {s.get('mistake_entries',0)} logged
Master prompt: v{s.get('master_prompt_version','?')}

Send any task. Getting smarter every run.""")
    threading.Thread(target=poll_queue, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"[CORE] Listening on port {PORT}")
    server.serve_forever()
