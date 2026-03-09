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

def notify(msg, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                   data={"chat_id": cid, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        print("[CORE] No RAILWAY_PUBLIC_DOMAIN, skipping webhook")
        return
    r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                   data={"url": f"https://{domain}/webhook"})
    print(f"[CORE] Webhook: {r.json()}")

def load_master_prompt():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/master_prompt?is_active=eq.true&order=version.desc&limit=1", headers=SB)
    data = r.json()
    if data:
        return data[0]["content"], data[0]["version"]
    return "", 0

def update_master_prompt(content, reason, score=90):
    httpx.patch(f"{SUPABASE_URL}/rest/v1/master_prompt?is_active=eq.true", headers=SB, json={"is_active": False})
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/master_prompt?order=version.desc&limit=1", headers=SB)
    data = r.json()
    next_v = (data[0]["version"] + 1) if data else 3
    httpx.post(f"{SUPABASE_URL}/rest/v1/master_prompt", headers=SB,
               json={"version": next_v, "content": content, "change_reason": reason, "quality_score": score, "is_active": True})
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
    print(f"[CORE] Synced master_prompt.md to GitHub v{version}")

def get_mistakes(domain):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/mistakes?domain=eq.{domain}", headers=SB)
    return r.json()

def store_pattern(domain, task_type, agents, score, services, notes):
    httpx.post(f"{SUPABASE_URL}/rest/v1/patterns", headers=SB, json={
        "domain": domain, "task_type": task_type, "agent_sequence": agents,
        "quality_score": score, "services_used": services, "notes": notes, "execution_time": 0})

def store_learning(summary, pattern, mistake, improvement):
    httpx.post(f"{SUPABASE_URL}/rest/v1/session_learning", headers=SB, json={
        "task_summary": summary, "new_pattern": pattern,
        "mistake_to_avoid": mistake, "estimated_improvement": improvement})

ORCHESTRATOR_PROMPT = 'You are CORE orchestrator. Output ONLY valid JSON: {"domain":"software","task_type":"web_app","agents":[{"role":"researcher","task":"research X"},{"role":"engineer","task":"build Y"}],"services":["supabase"]}'
CRITIC_PROMPT = 'You are CORE critic. Score 0-100. Output ONLY valid JSON: {"score":85,"issues":[],"verdict":"approved","improvement":""}'
PROMPT_EVOLVER = 'You are CORE prompt engineer. Given current master prompt and completed task, return full improved prompt OR exactly: NO_CHANGE. Be conservative, only improve if clear gap exists.'

AGENT_PROMPTS = {
    "researcher": "You are a world-class researcher. Be comprehensive and specific.",
    "planner": "You are a master project planner. Create clear phases and milestones.",
    "engineer": "You are a senior software engineer. Write clean production-ready code.",
    "designer": "You are a UI/UX designer. Create detailed design specifications.",
    "writer": "You are a professional technical writer. Produce clear structured documents.",
    "analyst": "You are a data analyst. Provide accurate calculations and breakdowns.",
    "qa": "You are a QA engineer. Review for quality, completeness, and edge cases."
}

def call_claude(system, user, context=""):
    content = f"{user}\n\nContext:\n{context}" if context else user
    r = httpx.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-5", "max_tokens": 4096, "system": system,
              "messages": [{"role": "user", "content": content}]}, timeout=90)
    data = r.json()
    return data["content"][0]["text"] if data.get("content") else ""

def execute_task(user_task, reply_chat_id=None):
    cid = reply_chat_id or TELEGRAM_CHAT_ID
    notify(f"CORE: Starting...\n{user_task[:80]}", cid)
    master_content, master_version = load_master_prompt()
    start = datetime.now()

    plan_raw = call_claude(ORCHESTRATOR_PROMPT, user_task)
    try: plan = json.loads(plan_raw)
    except: plan = {"domain":"general","task_type":"unknown","agents":[{"role":"writer","task":user_task}],"services":[]}

    domain = plan.get("domain","general")
    task_type = plan.get("task_type","unknown")
    agents = plan.get("agents",[])
    mistakes = get_mistakes(domain)
    mistakes_text = "\n".join([f"- {m['what_failed']}: {m['how_to_avoid']}" for m in mistakes]) or "None"
    context = f"Task: {user_task}\nMistakes:\n{mistakes_text}"
    results = {}

    for agent_def in agents:
        role = agent_def.get("role","writer")
        out = call_claude(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]), agent_def.get("task", user_task), context)
        results[role] = out
        context += f"\n\n{role.upper()} OUTPUT:\n{out}"

    try:
        critic = json.loads(call_claude(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}"))
        score = critic.get("score", 75)
    except: score, critic = 75, {"score":75,"issues":[]}

    attempts = 1
    while score < 85 and attempts < 3:
        role = agents[-1]["role"] if agents else "writer"
        retry = call_claude(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]),
                            f"Fix: {critic.get('issues',[])}. Task: {user_task}", context)
        context += f"\n\nIMPROVED:\n{retry}"
        try:
            critic = json.loads(call_claude(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}"))
            score = critic.get("score", 75)
        except: score = 75
        attempts += 1

    store_pattern(domain, task_type, [a["role"] for a in agents], score, plan.get("services",[]), user_task[:100])
    store_learning(user_task[:200], str([a["role"] for a in agents]), mistakes_text[:200], min(score/100,1.0))

    evolved = call_claude(PROMPT_EVOLVER, f"Current prompt:\n{master_content}\n\nTask: {user_task}\nAgents: {[a['role'] for a in agents]}\nScore: {score}")
    if evolved.strip() != "NO_CHANGE" and len(evolved) > 100:
        new_v = update_master_prompt(evolved, f"Auto-evolved: {user_task[:60]}", score)
        sync_to_github(evolved, new_v)
        notify(f"Master prompt evolved to v{new_v}", cid)

    duration = (datetime.now() - start).seconds
    first = list(results.values())[0][:400] if results else ""
    notify(f"CORE Task Done\nTask: {user_task[:60]}\nScore: {score}/100\nAgents: {', '.join([a['role'] for a in agents])}\nTime: {duration}s\n\nPreview:\n{first}...", cid)
    return context

def handle_message(message):
    chat_id = str(message.get("chat",{}).get("id",""))
    text = message.get("text","").strip()
    if not text: return
    print(f"[CORE] Telegram {chat_id}: {text[:60]}")

    if text == "/start":
        notify("CORE online\n\nSend any task to execute it.\n\n/status - system status\n/prompt - current prompt version\n/tasks - recent tasks", chat_id)
    elif text == "/status":
        _, v = load_master_prompt()
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/patterns?select=id", headers=SB)
        notify(f"CORE Status\nMaster prompt: v{v}\nPatterns: {len(r.json())}\nStatus: Online", chat_id)
    elif text == "/prompt":
        content, v = load_master_prompt()
        notify(f"Master Prompt v{v}\n\n{content[:600]}...", chat_id)
    elif text == "/tasks":
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/patterns?order=created_at.desc&limit=5", headers=SB)
        tasks = r.json()
        msg = "Recent Tasks\n\n" + "\n".join([f"- {t.get('notes','?')[:50]} (score:{t.get('quality_score',0)})" for t in tasks]) if tasks else "No tasks yet."
        notify(msg, chat_id)
    else:
        threading.Thread(target=execute_task, args=(text, chat_id), daemon=True).start()

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
        _, v = load_master_prompt()
        self.wfile.write(f"CORE online. Master prompt v{v}".encode())

    def log_message(self, *args): pass

def poll_queue():
    while True:
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/task_queue?status=eq.pending&order=priority.asc&limit=1", headers=SB)
            tasks = r.json()
            if tasks:
                task = tasks[0]
                httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB, json={"status":"running"})
                try:
                    result = execute_task(task["task"])
                    httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB, json={"status":"done","result":result[:5000]})
                except Exception as e:
                    httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB, json={"status":"failed","error":str(e)})
                    notify(f"CORE queue failed: {str(e)[:100]}")
        except Exception as e:
            print(f"[CORE] Poll error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    print(f"[CORE] Starting on port {PORT}")
    set_webhook()
    notify("CORE online. Send any task via Telegram.")
    threading.Thread(target=poll_queue, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"[CORE] Listening on port {PORT}")
    server.serve_forever()
