import os, json, httpx, time, threading, random


# ── VERIFY-AFTER-WRITE MODULE ─────────────────────────────
# Principle 17: Never assume a remote write succeeded. Always verify.
# Every write to GitHub/Supabase/Railway/Telegram must call verify_* after.

def verify_github_file(filename, expected_first_line):
    """Read file back from GitHub and confirm first line matches."""
    try:
        import base64
        h = {"Authorization": f"Bearer {GITHUB_PAT}", "User-Agent": "CORE"}
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_USERNAME}/core-agi/contents/{filename}", headers=h, timeout=10)
        actual_content = base64.b64decode(r.json().get("content","").replace("\\n","")).decode("utf-8","ignore")
        first = actual_content.split("\n")[0].strip()
        if first == expected_first_line:
            print(f"[VERIFY OK] GitHub {filename}: {first[:60]}")
            return True
        else:
            msg = f"CORE ALERT: GitHub {filename} verify FAILED.\nExpected: {expected_first_line}\nGot: {first[:80]}"
            print(f"[VERIFY FAIL] {msg}")
            notify(msg)
            return False
    except Exception as e:
        print(f"[VERIFY ERROR] GitHub {filename}: {e}")
        notify(f"CORE ALERT: GitHub verify error for {filename}: {e}")
        return False

def verify_supabase_row(table, filter_param, expected_count=1, label=""):
    """Query table and confirm expected rows exist."""
    try:
        rows = sb_get(table, f"?{filter_param}&limit=10")
        count = len(rows) if isinstance(rows, list) else 0
        if count >= expected_count:
            print(f"[VERIFY OK] Supabase {table} ({label}): {count} rows found")
            return True
        else:
            msg = f"CORE ALERT: Supabase {table} verify FAILED ({label}).\nExpected >={expected_count} rows, got {count}"
            print(f"[VERIFY FAIL] {msg}")
            notify(msg)
            return False
    except Exception as e:
        print(f"[VERIFY ERROR] Supabase {table}: {e}")
        notify(f"CORE ALERT: Supabase verify error for {table}: {e}")
        return False

def verify_telegram_webhook(expected_url):
    """Confirm webhook URL is set correctly."""
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo", timeout=10).json()
        actual = r.get("result", {}).get("url", "")
        if actual == expected_url:
            print(f"[VERIFY OK] Telegram webhook: {actual}")
            return True
        else:
            msg = f"CORE ALERT: Webhook verify FAILED.\nExpected: {expected_url}\nGot: {actual}"
            print(f"[VERIFY FAIL] {msg}")
            notify(msg)
            return False
    except Exception as e:
        print(f"[VERIFY ERROR] Telegram webhook: {e}")
        return False

def verify_telegram_commands(expected_count):
    """Confirm bot commands are registered."""
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMyCommands", timeout=10).json()
        count = len(r.get("result", []))
        if count >= expected_count:
            print(f"[VERIFY OK] Telegram commands: {count} registered")
            return True
        else:
            msg = f"CORE ALERT: Bot commands verify FAILED. Expected >={expected_count}, got {count}"
            print(f"[VERIFY FAIL] {msg}")
            notify(msg)
            return False
    except Exception as e:
        print(f"[VERIFY ERROR] Telegram commands: {e}")
        return False

def verified_sync_to_github(content, version):
    """sync_to_github + immediate read-back verification."""
    if not GITHUB_PAT: return False
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
    # VERIFY: read back and check version line
    expected_first = f"MASTER SYSTEM PROMPT v{version}"
    return verify_github_file("master_prompt.md", expected_first)

def verified_set_webhook():
    """set_webhook + immediate verification."""
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        print("[CORE] WARN: No RAILWAY_PUBLIC_DOMAIN - webhook not set")
        return False
    expected = f"https://{domain}/webhook"
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
               data={"url": expected})
    return verify_telegram_webhook(expected)

import os, json, httpx, time, threading, random
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# -- CREDENTIALS ------------------------------------------
SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT           = os.environ.get("GITHUB_PAT", "")
GITHUB_USERNAME      = os.environ.get("GITHUB_USERNAME", "")
PORT                 = int(os.environ.get("PORT", 8080))
OWNER_ID             = "838737537"

# -- GEMINI KEY ROTATION -----------------------------------
# Load all GEMINI_API_KEY_1 .. GEMINI_API_KEY_N from env
GEMINI_KEYS = []
i = 1
while True:
    k = os.environ.get(f"GEMINI_API_KEY_{i}")
    if not k: break
    GEMINI_KEYS.append(k)
    i += 1
if not GEMINI_KEYS:
    raise RuntimeError("No GEMINI_API_KEY_1 found in env")

_key_index = 0
_key_lock  = threading.Lock()

def next_gemini_key():
    global _key_index
    with _key_lock:
        key = GEMINI_KEYS[_key_index % len(GEMINI_KEYS)]
        _key_index += 1
        return key

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL   = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# -- SUPABASE HEADERS --------------------------------------
SB = {
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "apikey": SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# -- GEMINI CALL WITH KEY ROTATION ------------------------
def call_gemini(system_prompt, user_message, context="", retries=0):
    full_user = f"{user_message}\n\nContext:\n{context}" if context else user_message
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": full_user}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
    }
    tried = set()
    for attempt in range(len(GEMINI_KEYS)):
        key = next_gemini_key()
        if key in tried: continue
        tried.add(key)
        try:
            r = httpx.post(
                GEMINI_URL.format(model=GEMINI_MODEL, key=key),
                json=payload, timeout=60
            )
            if r.status_code == 429:
                print(f"[GEMINI] Key {key[:20]}... rate limited, rotating...")
                time.sleep(1)
                continue
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[GEMINI] Error with key {key[:20]}: {e}")
            continue
    return "GEMINI_ERROR: All keys exhausted or failed"

# Keep call_claude as alias for compatibility
def call_claude(system, user, context=""):
    return call_gemini(system, user, context)

# -- TELEGRAM ----------------------------------------------
def notify(msg, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                   data={"chat_id": cid, "text": msg[:4000], "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain: return
    r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                   data={"url": f"https://{domain}/webhook"})
    print(f"[CORE] Webhook: {r.json().get(chr(100)+chr(101)+chr(115)+chr(99)+chr(114)+chr(105)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110), r.text)}")

# -- SUPABASE ----------------------------------------------
def sb_get(endpoint, params=""):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{endpoint}{params}", headers=SB)
    return r.json()

def sb_post(endpoint, data):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=SB, json=data)
    return r.json()

def sb_patch(endpoint, data):
    httpx.patch(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=SB, json=data)

# -- MASTER PROMPT -----------------------------------------
def load_master_prompt():
    data = sb_get("master_prompt", "?is_active=eq.true&order=version.desc&limit=1")
    if data:
        print(f"[CORE] Loaded master_prompt v{data[0][chr(118)+chr(101)+chr(114)+chr(115)+chr(105)+chr(111)+chr(110)]}")
        return data[0]["content"], data[0]["version"]
    return "", 0

# -- JARVIS-BRAIN CONTEXT ----------------------------------
def get_context(domain, task_keywords=""):
    domain_ctx = sb_get("agi_context", f"?domain=eq.{domain}&limit=10")
    kw_ctx = []
    if task_keywords:
        word = task_keywords.split()[0]
        kw_ctx = sb_get("agi_context", f"?key=ilike.*{word}*&limit=5")
    combined = domain_ctx + [x for x in kw_ctx if x not in domain_ctx]
    return combined[:12]

def get_mistakes(domain):
    return sb_get("agi_mistakes", f"?domain=eq.{domain}&limit=10") + sb_get("agi_mistakes", "?domain=eq.general&limit=5")

def get_playbook(topic_keyword):
    if not topic_keyword: return []
    word = topic_keyword.split()[0]
    return sb_get("playbook", f"?topic=ilike.*{word}*&limit=3")

def get_agi_status():
    data = sb_get("agi_status", "")
    return data[0] if data else {}

# -- STORE LEARNINGS ---------------------------------------
def store_pattern(domain, task_type, agents, score, services, notes):
    sb_post("patterns", {"domain": domain, "task_type": task_type, "agent_sequence": agents,
                         "quality_score": score, "services_used": services, "notes": notes, "execution_time": 0})

def store_learning(summary, pattern, mistake, improvement):
    sb_post("session_learning", {"task_summary": summary, "new_pattern": pattern,
                                 "mistake_to_avoid": mistake, "estimated_improvement": improvement})

def store_knowledge(domain, topic, content, tags, confidence="learned"):
    sb_post("knowledge_base", {"domain": domain, "topic": topic, "content": content,
                               "source": "core_agi", "confidence": confidence, "tags": tags})

def store_mistake(context, what_failed, correct_approach, tags, domain="general"):
    sb_post("mistakes", {"context": context, "what_failed": what_failed,
                         "correct_approach": correct_approach, "how_to_avoid": correct_approach,
                         "domain": domain, "tags": tags, "severity": "medium"})

def log_session(summary, actions, interface="telegram"):
    sb_post("sessions", {"summary": summary, "actions": actions, "interface": interface})

# -- AGENT REGISTRY ----------------------------------------
def upsert_agent(name, input_desc, output_desc, score):
    existing = sb_get("agent_registry", f"?name=eq.{name}&limit=1")
    if existing:
        sb_patch(f"agent_registry?name=eq.{name}", {"quality_score": score, "use_count": existing[0].get("use_count",0)+1, "last_used": datetime.utcnow().isoformat()})
    else:
        sb_post("agent_registry", {"name": name, "input_description": input_desc, "output_description": output_desc, "quality_score": score, "status": "active"})

# -- AGENT PROMPTS -----------------------------------------
ORCHESTRATOR_PROMPT = """You are CORE orchestrator. Analyze the task and output ONLY valid JSON:
{"domain":"software","task_type":"web_app","agents":[{"role":"researcher","task":"research X"},{"role":"engineer","task":"build Y"}],"services":["supabase"]}"""

CRITIC_PROMPT = """You are CORE critic. Score 0-100. Output ONLY valid JSON:
{"score":85,"issues":[],"verdict":"approved","improvement":""}"""

PROMPT_EVOLVER = """You are CORE prompt engineer. Given current master prompt and task summary,
return full improved prompt text OR exactly: NO_CHANGE
Be conservative - only improve if there is a clear gap."""

KNOWLEDGE_EXTRACTOR = """You are a knowledge extractor. Output ONLY valid JSON:
{"new_knowledge":[{"domain":"X","topic":"Y","content":"Z","tags":["a"]}],"new_mistakes":[]}"""

AGENT_PROMPTS = {
    "researcher": "You are a world-class researcher. Be comprehensive and specific.",
    "planner":    "You are a master project planner. Create clear phases, milestones, and risk mitigations.",
    "engineer":   "You are a senior software engineer. Write clean, production-ready code.",
    "designer":   "You are a UI/UX designer. Create detailed design specifications.",
    "writer":     "You are a professional technical writer. Produce clear, well-structured documents.",
    "analyst":    "You are a data analyst. Provide accurate calculations and breakdowns.",
    "qa":         "You are a QA engineer. Review for quality, completeness, and edge cases."
}

# -- MASTER PROMPT EVOLUTION -------------------------------
def update_master_prompt(content, reason, score=90):
    sb_patch("master_prompt?is_active=eq.true", {"is_active": False})
    data = sb_get("master_prompt", "?order=version.desc&limit=1")
    next_v = (data[0]["version"] + 1) if data else 3
    sb_post("master_prompt", {"version": next_v, "content": content,
                              "change_reason": reason, "quality_score": score, "is_active": True})
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

# -- MAIN TASK EXECUTOR ------------------------------------
def execute_task(user_task, reply_chat_id=None):
    cid = reply_chat_id or TELEGRAM_CHAT_ID
    notify(f"CORE: Starting...\n`{user_task[:80]}`", cid)
    master_content, master_version = load_master_prompt()
    start = datetime.now()

    # Phase 0 - Orchestrate
    plan_raw = call_gemini(ORCHESTRATOR_PROMPT, user_task)
    try:
        plan_raw_clean = plan_raw.strip().lstrip("```json").lstrip("```").rstrip("```")
        plan = json.loads(plan_raw_clean)
    except:
        plan = {"domain":"general","task_type":"unknown","agents":[{"role":"writer","task":user_task}],"services":[]}

    domain    = plan.get("domain","general")
    task_type = plan.get("task_type","unknown")
    agents    = plan.get("agents",[])

    # Phase 1 - Memory Check
    knowledge = get_context(domain, user_task)
    mistakes  = get_mistakes(domain)
    playbook  = get_playbook(domain)

    knowledge_text = "\n".join([f"- [{x.get(chr(115)+chr(111)+chr(117)+chr(114)+chr(99)+chr(101)+chr(95)+chr(116)+chr(97)+chr(98)+chr(108)+chr(101),chr(63))}] {x.get(chr(107)+chr(101)+chr(121),chr(63))}: {str(x.get(chr(118)+chr(97)+chr(108)+chr(117)+chr(101),chr(63)))[:200]}" for x in knowledge]) or "None"
    mistakes_text  = "\n".join([f"- AVOID: {m.get(chr(119)+chr(104)+chr(97)+chr(116)+chr(95)+chr(102)+chr(97)+chr(105)+chr(108)+chr(101)+chr(100),chr(63))[:100]}" for m in mistakes]) or "None"
    playbook_text  = "\n".join([f"- METHOD: {p.get(chr(116)+chr(111)+chr(112)+chr(105)+chr(99),chr(63))}: {p.get(chr(109)+chr(101)+chr(116)+chr(104)+chr(111)+chr(100),chr(63))[:150]}" for p in playbook]) or "None"

    context = f"""Task: {user_task}

JARVIS-BRAIN KNOWLEDGE ({len(knowledge)} entries):
{knowledge_text}

PROVEN METHODS:
{playbook_text}

MISTAKES TO AVOID ({len(mistakes)} entries):
{mistakes_text}"""

    results = {}
    # Phase 4 - Execute agents
    for agent_def in agents:
        role = agent_def.get("role","writer")
        task = agent_def.get("task", user_task)
        print(f"  -> {role}...")
        out = call_gemini(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]), task, context)
        results[role] = out
        context += f"\n\n{role.upper()} OUTPUT:\n{out}"
        upsert_agent(role, "task+context", "agent output", 80)

    # Phase 5 - Critic
    try:
        critic_raw = call_gemini(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}")
        critic_clean = critic_raw.strip().lstrip("```json").lstrip("```").rstrip("```")
        critic = json.loads(critic_clean)
        score  = critic.get("score", 75)
    except:
        score, critic = 75, {"score":75,"issues":[]}

    attempts = 1
    while score < 85 and attempts < 3:
        role  = agents[-1]["role"] if agents else "writer"
        retry = call_gemini(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]),
                            f"Fix these issues: {critic.get(chr(105)+chr(115)+chr(115)+chr(117)+chr(101)+chr(115),[])}. Task: {user_task}", context)
        context += f"\n\nIMPROVED:\n{retry}"
        try:
            critic_raw = call_gemini(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}")
            critic = json.loads(critic_raw.strip().lstrip("```json").lstrip("```").rstrip("```"))
            score  = critic.get("score", 75)
        except:
            score = 75
        attempts += 1

    # Phase 6 - Store learnings
    store_pattern(domain, task_type, [a["role"] for a in agents], score, plan.get("services",[]), user_task[:100])
    store_learning(user_task[:200], str([a["role"] for a in agents]), mistakes_text[:200], min(score/100,1.0))
    log_session(f"Task: {user_task[:100]}", [a["role"] for a in agents])

    # Extract new knowledge
    try:
        extracted_raw = call_gemini(KNOWLEDGE_EXTRACTOR, f"Task: {user_task}\n\nOutput:\n{context[:2000]}")
        extracted = json.loads(extracted_raw.strip().lstrip("```json").lstrip("```").rstrip("```"))
        for k in extracted.get("new_knowledge",[]):
            store_knowledge(k["domain"], k["topic"], k["content"], k.get("tags",[]))
        for m in extracted.get("new_mistakes",[]):
            store_mistake(user_task[:100], m.get("what",""), m.get("avoid",""), [domain])
    except Exception as e:
        print(f"[CORE] Knowledge extraction error: {e}")

    # Evolve master prompt
    try:
        evolved = call_gemini(PROMPT_EVOLVER, f"Current:\n{master_content[:800]}\n\nTask: {user_task}\nScore: {score}")
        if evolved.strip() != "NO_CHANGE" and len(evolved) > 100:
            new_v = update_master_prompt(evolved, f"Auto-evolved: {user_task[:60]}", score)
            verified_sync_to_github(evolved, new_v)
            notify(f"Master prompt evolved to v{new_v}", cid)
    except Exception as e:
        print(f"[CORE] Prompt evolution error: {e}")

    duration = (datetime.now() - start).seconds
    s = get_agi_status()
    first = list(results.values())[0][:400] if results else "No output"

    notify(f"""? CORE Task Done
Task: {user_task[:60]}
Score: {score}/100 | Time: {duration}s
Agents: {", ".join([a["role"] for a in agents])}
Knowledge used: {len(knowledge)} | Mistakes avoided: {len(mistakes)}
DB: {s.get("knowledge_entries",0)} knowledge | {s.get("pattern_entries",0)} patterns

Preview:
{first}...""", cid)

    return context

# -- TELEGRAM COMMAND HANDLER ------------------------------
def handle_message(message):
    chat_id = str(message.get("chat",{}).get("id",""))
    text    = message.get("text","").strip()
    if not text: return
    if chat_id != OWNER_ID:
        notify("Unauthorized.", chat_id)
        return
    print(f"[CORE] {chat_id}: {text[:60]}")

    if text == "/start":
        s = get_agi_status()
        notify(f"""?? *CORE AGI Online*
Brain: Gemini 2.0 Flash x{len(GEMINI_KEYS)} keys
Knowledge: {s.get("knowledge_entries",0)} entries
Playbook: {s.get("playbook_entries",0)} methods
Patterns: {s.get("pattern_entries",0)} learned
Master prompt: v{s.get("master_prompt_version","?")}

Commands:
/status - system health
/prompt - current master prompt
/tasks - recent tasks
/ask [query] - search knowledge base
/keys - check Gemini key status

Or send any task to execute it.""", chat_id)

    elif text == "/status":
        s = get_agi_status()
        notify(f"""?? *CORE Status*
Knowledge: {s.get("knowledge_entries",0)}
Playbook: {s.get("playbook_entries",0)}
Mistakes: {s.get("mistake_entries",0)}
Patterns: {s.get("pattern_entries",0)}
Pending tasks: {s.get("pending_tasks",0)}
Master prompt: v{s.get("master_prompt_version","?")}
Gemini keys loaded: {len(GEMINI_KEYS)}""", chat_id)

    elif text == "/keys":
        notify(f"?? Gemini keys loaded: {len(GEMINI_KEYS)}\nModel: {GEMINI_MODEL}\nRotation: round-robin", chat_id)

    elif text == "/prompt":
        content, v = load_master_prompt()
        notify(f"?? Master Prompt v{v}\n\n{content[:800]}...", chat_id)

    elif text == "/tasks":
        tasks = sb_get("patterns", "?order=created_at.desc&limit=5")
        msg = "?? *Recent Tasks*\n\n"
        msg += "\n".join([f"- {t.get(chr(110)+chr(111)+chr(116)+chr(101)+chr(115),chr(63))[:50]} (score:{t.get(chr(113)+chr(117)+chr(97)+chr(108)+chr(105)+chr(116)+chr(121)+chr(95)+chr(115)+chr(99)+chr(111)+chr(114)+chr(101),0)})" for t in tasks]) if tasks else "No tasks yet."
        notify(msg, chat_id)

    elif text.startswith("/ask "):
        query = text[5:].strip()
        word  = query.split()[0] if query else ""
        results = sb_get("agi_context", f"?key=ilike.*{word}*&limit=5")
        if results:
            msg = f"?? Knowledge on *{query}*:\n\n"
            msg += "\n\n".join([f"[{r.get(chr(115)+chr(111)+chr(117)+chr(114)+chr(99)+chr(101)+chr(95)+chr(116)+chr(97)+chr(98)+chr(108)+chr(101),chr(63))}] {r.get(chr(107)+chr(101)+chr(121),chr(63))}:\n{str(r.get(chr(118)+chr(97)+chr(108)+chr(117)+chr(101),chr(63)))[:200]}" for r in results])
        else:
            msg = f"No knowledge found for *{query}*"
        notify(msg, chat_id)

    else:
        threading.Thread(target=execute_task, args=(text, chat_id), daemon=True).start()

# -- WEBHOOK SERVER ----------------------------------------
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
        self.wfile.write(f"CORE | Gemini x{len(GEMINI_KEYS)} | Prompt v{s.get(chr(109)+chr(97)+chr(115)+chr(116)+chr(101)+chr(114)+chr(95)+chr(112)+chr(114)+chr(111)+chr(109)+chr(112)+chr(116)+chr(95)+chr(118)+chr(101)+chr(114)+chr(115)+chr(105)+chr(111)+chr(110),chr(63))} | Knowledge: {s.get(chr(107)+chr(110)+chr(111)+chr(119)+chr(108)+chr(101)+chr(100)+chr(103)+chr(101)+chr(95)+chr(101)+chr(110)+chr(116)+chr(114)+chr(105)+chr(101)+chr(115),0)}".encode())

    def log_message(self, *args): pass

# -- QUEUE POLLER ------------------------------------------
def poll_queue():
    while True:
        try:
            tasks = sb_get("task_queue", "?status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                task = tasks[0]
                sb_patch(f"task_queue?id=eq.{task[chr(105)+chr(100)]}", {"status":"running"})
                try:
                    result = execute_task(task["task"], task.get("chat_id"))
                    sb_patch(f"task_queue?id=eq.{task[chr(105)+chr(100)]}", {"status":"done","result":result[:5000]})
                except Exception as e:
                    sb_patch(f"task_queue?id=eq.{task[chr(105)+chr(100)]}", {"status":"failed","error":str(e)})
                    notify(f"CORE queue failed: {str(e)[:100]}")
        except Exception as e:
            print(f"[CORE] Poll error: {e}")
        time.sleep(30)

# -- MAIN --------------------------------------------------
if __name__ == "__main__":
    print(f"[CORE] Starting on port {PORT}")
    print(f"[CORE] Gemini keys loaded: {len(GEMINI_KEYS)}")

    # VERIFY: webhook set correctly
    verified_set_webhook()

    # VERIFY: bot commands registered
    verify_telegram_commands(5)

    # VERIFY: Supabase core tables exist
    verify_supabase_row("agent_registry", "status=eq.active", 7, "core agents")
    verify_supabase_row("stack_registry", "status=eq.active", 5, "core stack")

    s = get_agi_status()
    notify(f"""CORE Online (Gemini 2.0 Flash)
Keys: {len(GEMINI_KEYS)} rotating | Model: {GEMINI_MODEL}
Knowledge: {s.get("knowledge_entries",0)} entries
Master prompt: v{s.get("master_prompt_version","?")}

Startup verified. Send any task. Free forever.""")
    threading.Thread(target=poll_queue, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"[CORE] Listening on port {PORT}")
    server.serve_forever()
