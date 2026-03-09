﻿import os, json, httpx, time, threading, random, urllib.request, hashlib
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

# -- VAULT BOOT -------------------------------------------
VAULT_URL = "https://core-vault.pockiesaints7.workers.dev/v2/52c27a1a462e34878037926090ee7e833986622b17032057a3d6bbcddd1e804e"
EXPECTED_HASH = "92402b81f147181e50778f9d5ab4b0a18fa001f952a41467e1fe4989acd989eb"
_vault_config = {}

def boot_from_vault():
    """Fetch master_prompt.md + vault topology on every startup. CORE identity."""
    global _vault_config
    try:
        # Step 1: Fetch master_prompt.md
        mp_url = "https://raw.githubusercontent.com/pockiesaints7/core-agi/main/master_prompt.md"
        mp_req = urllib.request.Request(mp_url, headers={"User-Agent": "CORE-AGI"})
        mp_content = urllib.request.urlopen(mp_req).read().decode()
        version = mp_content.split("\n")[0].split("v")[1].split(" ")[0] if "v" in mp_content.split("\n")[0] else "?"
        print(f"[BOOT] master_prompt.md loaded v{version}")

        # Step 2: Fetch vault topology
        v_req = urllib.request.Request(VAULT_URL, headers={"User-Agent": "CORE-AGI"})
        vault_data = json.loads(urllib.request.urlopen(v_req).read().decode())
        _vault_config = vault_data
        print(f"[BOOT] Vault loaded: version={vault_data.get('version')} hash={vault_data.get('prompt_hash','')[:12]}...")

        # Step 3: Verify hash
        actual_hash = hashlib.sha256(mp_content.encode()).hexdigest()
        if actual_hash != vault_data.get("prompt_hash", ""):
            msg = f"CORE TAMPER ALERT: prompt_hash mismatch!\nExpected: {vault_data.get('prompt_hash','')[:20]}\nActual: {actual_hash[:20]}"
            print(f"[BOOT] {msg}")
            notify(msg)
        else:
            print(f"[BOOT] Hash verified OK")

        # Store master_prompt as active system context
        _vault_config["master_prompt_content"] = mp_content
        _vault_config["booted_version"] = version
        return mp_content, vault_data

    except Exception as e:
        print(f"[BOOT ERROR] {e}")
        notify(f"CORE BOOT WARNING: vault fetch failed - {e}\nRunning in degraded mode.")
        return "", {}

def get_system_prompt():
    """Return master_prompt content as system prompt for Gemini calls."""
    return _vault_config.get("master_prompt_content", "") or "You are CORE, a universal AGI execution system."



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
        raw = data[0].get("content", "")
        # Unwrap if stored as JSON {"value": "..."}
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
                raw = parsed.get("value", raw)
            except Exception:
                pass
        elif isinstance(raw, dict):
            raw = raw.get("value", str(raw))
        version = data[0].get("version", 0)
        print(f"[CORE] Loaded master_prompt v{version} ({len(raw)} chars)")
        return raw, version
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
    # Use vault system prompt as primary; fall back to Supabase version trimmed
    vault_prompt = get_system_prompt()
    effective_system = vault_prompt if vault_prompt else (master_content[:3000] if master_content else "You are CORE AGI, a universal task execution system. Be thorough and helpful.")
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
            push_to_github("master_prompt.md", evolved, f"CORE auto-evolve v{new_v}", f"MASTER SYSTEM PROMPT v{new_v}")
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

    # PHASE 6b - HOT REFLECTION (autonomous, no trigger needed)
    try:
        hot_reflect(user_task[:200], domain, [a["role"] for a in agents], score, len(mistakes) > 0, 1)
    except Exception as e:
        print(f"[HOT REFLECT SKIP] {e}")

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
        # Detect if this is a quick conversational message or a real task
        quick_keywords = len(text.split()) <= 6 and not any(k in text.lower() for k in [
            "build", "create", "write", "analyze", "make", "generate", "research",
            "plan", "code", "design", "fix", "debug", "deploy", "setup"
        ])
        if quick_keywords:
            # Fast conversational reply via Gemini directly
            def quick_reply():
                system = get_system_prompt() or "You are CORE, an AI assistant created by REINVAGNAR. Be concise and helpful."
                reply = call_gemini(system, text)
                notify(reply[:4000], chat_id)
            threading.Thread(target=quick_reply, daemon=True).start()
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
COLD_REFLECT_INTERVAL = 6 * 3600  # 6 hours
_last_cold = [0]

def poll_queue():
    global _last_cold
    while True:
        try:
            # Cold reflection every 6h
            import time as _t
            if _t.time() - _last_cold[0] > COLD_REFLECT_INTERVAL:
                _last_cold[0] = _t.time()
                threading.Thread(target=cold_reflect, daemon=True).start()
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

# ══════════════════════════════════════════════════════════
# CORE AGI - ANTI-HALLUCINATION + SELF-LEARNING SYSTEM
# Principle 17: Never assume. Always verify.
# Principle 18: Every failure writes to mistakes DB.
# Principle 19: Every operation reads mistakes DB first.
# ══════════════════════════════════════════════════════════

# ── MISTAKE GUARD - read before every operation type ──────
def get_mistakes_for_domain(domain):
    """Query mistakes DB before any operation. CORE learns from past failures."""
    try:
        rows = sb_get("mistakes", f"?domain=eq.{domain}&order=severity.desc&limit=5")
        if rows:
            print(f"[MISTAKE_GUARD] {len(rows)} known mistakes for domain={domain}:")
            for r in rows:
                print(f"  AVOID: {r.get('what_failed','')[:80]}")
                print(f"  DO: {r.get('correct_approach','')[:80]}")
        return rows
    except Exception as e:
        print(f"[MISTAKE_GUARD] Could not load mistakes for {domain}: {e}")
        return []

def store_mistake_now(domain, what_failed, root_cause, correct_approach, tags, severity="high"):
    """Immediately store a failure to mistakes DB so it never repeats."""
    try:
        sb_post("mistakes", {
            "domain": domain,
            "context": f"CORE operation: {domain}",
            "what_failed": what_failed,
            "root_cause": root_cause,
            "correct_approach": correct_approach,
            "how_to_avoid": correct_approach,
            "tags": tags,
            "severity": severity
        })
        print(f"[MISTAKE_STORED] [{domain}] {what_failed[:60]}")
    except Exception as e:
        print(f"[MISTAKE_STORE_ERROR] {e}")

# ── REMOTE_OP - universal write+verify wrapper ─────────────
def remote_op(op_name, domain, write_fn, verify_fn, on_fail_advice=""):
    """
    Universal pattern for ALL remote write operations.
    1. Read mistakes for this domain first
    2. Execute write
    3. Verify immediately
    4. On fail: store to mistakes DB + notify owner
    5. Never report success without verify confirmation
    """
    # Step 1: consult mistakes DB before acting
    get_mistakes_for_domain(domain)

    # Step 2: execute write
    try:
        write_result = write_fn()
    except Exception as e:
        msg = f"CORE ALERT: {op_name} write EXCEPTION: {e}"
        print(f"[REMOTE_OP FAIL] {msg}")
        notify(msg)
        store_mistake_now(domain, f"{op_name} threw exception: {str(e)[:100]}",
                         "Unexpected exception during write", on_fail_advice or str(e), [domain, "exception"])
        return False

    # Step 3: verify
    try:
        ok = verify_fn(write_result)
    except Exception as e:
        ok = False
        print(f"[REMOTE_OP] Verify function threw: {e}")

    # Step 4: handle result
    if ok:
        print(f"[REMOTE_OP OK] {op_name} verified successfully")
        return True
    else:
        msg = f"CORE ALERT: {op_name} VERIFY FAILED.\n{on_fail_advice}"
        print(f"[REMOTE_OP FAIL] {op_name}")
        notify(msg)
        store_mistake_now(domain,
            f"{op_name} passed but verify failed - assumed success was wrong",
            "Operation returned success but read-back showed different state",
            on_fail_advice or f"Always verify {op_name} with read-back",
            [domain, "verify-fail", "silent-failure"])
        return False

# ── VERIFIED GITHUB PUSH (uses remote_op) ─────────────────
def push_to_github(filename, new_content, commit_msg, expected_first_line):
    """Always fetch fresh SHA, push, verify. Uses remote_op wrapper."""
    get_mistakes_for_domain("github")  # consult before acting
    import base64
    repo = f"{GITHUB_USERNAME}/core-agi"
    h = {"Authorization": f"Bearer {GITHUB_PAT}", "Content-Type": "application/json", "User-Agent": "CORE"}

    def do_write():
        # Always fetch fresh SHA immediately before push - never reuse stale SHA
        fresh = httpx.get(f"https://api.github.com/repos/{repo}/contents/{filename}", headers=h, timeout=10).json()
        fresh_sha = fresh.get("sha")
        encoded = base64.b64encode(new_content.encode()).decode()
        payload = {"message": commit_msg, "content": encoded, "branch": "main"}
        if fresh_sha: payload["sha"] = fresh_sha
        r = httpx.put(f"https://api.github.com/repos/{repo}/contents/{filename}", headers=h, json=payload, timeout=15)
        return r.json()

    def do_verify(write_result):
        import time; time.sleep(2)  # brief settle
        check = httpx.get(f"https://api.github.com/repos/{repo}/contents/{filename}?t={int(time.time())}", headers=h, timeout=10).json()
        actual_content = base64.b64decode(check.get("content","").replace("\n","")).decode("utf-8","ignore")
        first = actual_content.split("\n")[0].strip()
        if first == expected_first_line:
            print(f"[VERIFY OK] GitHub {filename} first line: {first[:60]}")
            return True
        print(f"[VERIFY FAIL] GitHub {filename}: expected '{expected_first_line}' got '{first[:60]}'")
        return False

    return remote_op(f"GitHub push {filename}", "github", do_write, do_verify,
                     "Fetch fresh SHA immediately before push. Never reuse stale SHA from earlier in session.")

# ── VERIFIED SUPABASE WRITE (uses remote_op) ──────────────
def write_to_supabase(table, data, verify_filter, verify_label=""):
    """Post to Supabase + verify row exists. Uses remote_op wrapper."""
    get_mistakes_for_domain("supabase")

    def do_write():
        return sb_post(table, data)

    def do_verify(_):
        rows = sb_get(table, f"?{verify_filter}&limit=1")
        return isinstance(rows, list) and len(rows) > 0

    return remote_op(f"Supabase write {table}", "supabase", do_write, do_verify,
                     f"Always SELECT after INSERT to confirm row exists in {table}.")

# ── VERIFIED TELEGRAM COMMANDS (uses remote_op) ───────────
def register_bot_commands(commands_list):
    """Register commands + verify they stuck. Uses remote_op wrapper."""
    get_mistakes_for_domain("telegram")

    def do_write():
        import json
        r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
                       content=json.dumps(commands_list), headers={"Content-Type": "application/json"}, timeout=10)
        return r.json()

    def do_verify(_):
        import time; time.sleep(1)
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMyCommands", timeout=10).json()
        count = len(r.get("result", []))
        expected = len(commands_list)
        if count >= expected:
            print(f"[VERIFY OK] Bot commands: {count} registered")
            return True
        print(f"[VERIFY FAIL] Bot commands: expected {expected}, got {count}")
        return False

    return remote_op("Telegram setMyCommands", "telegram", do_write, do_verify,
                     "Re-register commands after every deploy. Redeploys wipe command registrations.")

BOT_COMMANDS = [
    {"command": "start",  "description": "System status and welcome"},
    {"command": "status", "description": "Full system health"},
    {"command": "prompt", "description": "Current master prompt"},
    {"command": "tasks",  "description": "Recent tasks"},
    {"command": "ask",    "description": "Search knowledge base: /ask [query]"},
    {"command": "keys",   "description": "Check Gemini key rotation status"}
]


# ══════════════════════════════════════════════════════════════
# CORE AGI - AUTONOMOUS REFLECTION ENGINE v1.0
# Hot reflection: fires after every task (no trigger needed)
# Cold reflection: every 6h synthesis + auto-evolution
# Confidence gate: >0.85 auto-apply | 0.60-0.85 propose to owner
# ══════════════════════════════════════════════════════════════

REFLECTION_PROMPT = """You are CORE reflection engine. Analyze task execution and output ONLY valid JSON:
{"verify_rate":0.0,"mistake_consult_rate":0.0,"quality_score":0.0,"gaps_identified":[],"new_patterns":[{"key":"snake_case","domain":"str","description":"str","confidence":0.0}],"reflection_text":"insight"}
Output ONLY the JSON. No explanation."""

COLD_PROMPT = """You are CORE synthesis engine. Given hot reflections batch, find patterns and propose prompt improvements. Output ONLY valid JSON:
{"patterns_found":[{"key":"str","domain":"str","description":"str","frequency":1,"confidence":0.0}],"evolution_proposals":[{"confidence":0.0,"impact":"high|medium|low","reversible":true,"change_type":"new_principle","change_summary":"str","diff_content":"str"}],"summary_text":"str"}
Output ONLY the JSON."""

def hot_reflect(task_summary, domain, used_agents, quality_score, mistakes_consulted, items_verified):
    """Auto-runs after every execute_task. No human trigger needed."""
    try:
        prompt_input = f"Task: {task_summary[:200]}\nDomain: {domain}\nAgents: {used_agents}\nQuality: {quality_score}\nMistakes consulted: {mistakes_consulted}\nItems verified: {items_verified}"
        raw = call_gemini(REFLECTION_PROMPT, prompt_input)
        r = json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```"))
        rec = {
            "task_summary": task_summary[:200], "domain": domain,
            "verify_rate": r.get("verify_rate", 0.5),
            "mistake_consult_rate": r.get("mistake_consult_rate", float(mistakes_consulted)),
            "new_patterns": json.dumps(r.get("new_patterns", [])),
            "new_mistakes": json.dumps([]),
            "quality_score": r.get("quality_score", quality_score/100),
            "gaps_identified": r.get("gaps_identified", []),
            "reflection_text": r.get("reflection_text", "")
        }
        sb_post("hot_reflections", rec)
        print(f"[HOT REFLECT] q={rec['quality_score']:.2f} | {rec['reflection_text'][:80]}")
        for p in r.get("new_patterns", []):
            _upsert_pattern(p)
        _check_evolution_threshold()
    except Exception as e:
        print(f"[HOT REFLECT ERROR] {e}")

def _upsert_pattern(p):
    try:
        existing = sb_get("pattern_frequency", f"?pattern_key=eq.{p['key']}&limit=1")
        if existing:
            row = existing[0]
            new_freq = row["frequency"] + 1
            new_conf = min(1.0, (row["confidence"] + p.get("confidence", 0.5)) / 2 + 0.05)
            httpx.patch(f"{SUPABASE_URL}/rest/v1/pattern_frequency?id=eq.{row['id']}", headers=SB,
                       json={"frequency": new_freq, "confidence": new_conf, "last_seen": datetime.now().isoformat()})
        else:
            sb_post("pattern_frequency", {"pattern_key": p["key"], "domain": p.get("domain","general"), "description": p.get("description",""), "frequency": 1, "confidence": p.get("confidence", 0.5)})
    except Exception as e:
        print(f"[PATTERN ERROR] {e}")

def _check_evolution_threshold(freq=3, conf=0.80):
    try:
        ready = sb_get("pattern_frequency", f"?frequency=gte.{freq}&confidence=gte.{conf}&auto_applied=eq.false&limit=5")
        for p in ready:
            existing = sb_get("evolution_queue", f"?pattern_key=eq.{p['pattern_key']}&status=eq.pending&limit=1")
            if not existing:
                sb_post("evolution_queue", {"status":"pending","confidence":p["confidence"],"impact":"medium","reversible":True,"change_type":"new_principle","change_summary":f"Pattern {p['pattern_key']} seen {p['frequency']}x","diff_content":p["description"],"pattern_key":p["pattern_key"],"frequency":p["frequency"]})
                notify(f"CORE EVOLUTION QUEUED\nPattern: {p['pattern_key']}\nSeen {p['frequency']}x @ {p['confidence']:.0%}\n\n{p['description'][:200]}")
    except Exception as e:
        print(f"[EVOLUTION THRESHOLD ERROR] {e}")

LAST_COLD_REFLECT = [None]

def cold_reflect():
    """Synthesizes hot reflections every 6h. Auto-evolves master prompt."""
    try:
        from datetime import timedelta
        period_start = (datetime.now() - timedelta(hours=6)).isoformat()
        hot = sb_get("hot_reflections", f"?processed_by_cold=eq.false&created_at=gte.{period_start}&limit=50")
        if not hot:
            return
        print(f"[COLD REFLECT] Processing {len(hot)} reflections...")
        lines = [f"[{h['domain']}] q={h['quality_score']:.2f} v={h['verify_rate']:.2f} | {h['reflection_text']}" for h in hot]
        raw = call_gemini(COLD_PROMPT, "Hot reflections:\n" + "\n".join(lines[:40]))
        s = json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```"))
        for p in s.get("patterns_found", []):
            _upsert_pattern(p)
        auto_applied = 0
        for prop in s.get("evolution_proposals", []):
            c = prop.get("confidence", 0)
            if c >= 0.85 and prop.get("reversible", True):
                _auto_apply_evolution(prop)
                auto_applied += 1
            elif c >= 0.60:
                sb_post("evolution_queue", {"status":"pending","confidence":c,"impact":prop.get("impact","medium"),"reversible":prop.get("reversible",True),"change_type":prop.get("change_type","new_principle"),"change_summary":prop.get("change_summary",""),"diff_content":prop.get("diff_content",""),"pattern_key":"cold_synthesis"})
                notify(f"CORE EVOLUTION PROPOSAL\nConf: {c:.0%} | {prop.get('change_summary','')[:100]}\n\nDiff: {prop.get('diff_content','')[:200]}")
        httpx.patch(f"{SUPABASE_URL}/rest/v1/hot_reflections?processed_by_cold=eq.false&created_at=gte.{period_start}", headers=SB, json={"processed_by_cold": True})
        sb_post("cold_reflections", {"period_start":period_start,"period_end":datetime.now().isoformat(),"hot_count":len(hot),"patterns_found":len(s.get("patterns_found",[])),"evolutions_queued":len(s.get("evolution_proposals",[])),"auto_applied":auto_applied,"summary_text":s.get("summary_text","")})
        notify(f"CORE Cold Reflect Done\nHot processed: {len(hot)} | Patterns: {len(s.get('patterns_found',[]))}\nAuto-applied: {auto_applied}\n\n{s.get('summary_text','')[:200]}")
    except Exception as e:
        print(f"[COLD REFLECT ERROR] {e}")

def _auto_apply_evolution(proposal):
    try:
        content, version = load_master_prompt()
        new_v = version + 1
        new_content = content + f"\n\n[AUTO-EVOLVED v{new_v}] {proposal.get('change_summary','')}\n{proposal.get('diff_content','')}"
        update_master_prompt(new_content, f"Cold reflect auto-evolve: {proposal.get('change_summary','')[:60]}", 90)
        push_to_github("master_prompt.md", new_content, f"CORE auto-evolve v{new_v}", f"MASTER SYSTEM PROMPT v{new_v}")
        notify(f"CORE AUTO-EVOLVED v{new_v}\n{proposal.get('change_summary','')[:100]}")
        print(f"[AUTO-EVOLVE] v{new_v}")
    except Exception as e:
        print(f"[AUTO-EVOLVE ERROR] {e}")


if __name__ == "__main__":
    print(f"[CORE] Starting on port {PORT}")
    print(f"[CORE] Gemini keys loaded: {len(GEMINI_KEYS)}")

    # Boot: load master_prompt + vault topology
    boot_from_vault()

    # Consult mistakes DB before startup operations
    get_mistakes_for_domain("telegram")
    get_mistakes_for_domain("railway")

    # Verified webhook set
    verified_set_webhook()

    # Register + verify bot commands (always on boot - redeploys wipe them)
    register_bot_commands(BOT_COMMANDS)

    # Verify core Supabase tables
    verify_supabase_row("agent_registry", "status=eq.active", 7, "core agents")
    verify_supabase_row("stack_registry", "status=eq.active", 5, "core stack")

    s = get_agi_status()
    notify(f"""CORE Online (Gemini 2.0 Flash)
Keys: {len(GEMINI_KEYS)} | Model: {GEMINI_MODEL}
Knowledge: {s.get("knowledge_entries",0)} | Prompt: v{s.get("master_prompt_version","?")}
Anti-hallucination: ACTIVE | Mistake guard: ACTIVE
Send any task.""")
    threading.Thread(target=poll_queue, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"[CORE] Listening on port {PORT}")
    server.serve_forever()

