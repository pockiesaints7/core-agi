import os, json, httpx, time
from datetime import datetime

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
JARVIS_OS_URL = os.environ.get("JARVIS_OS_URL", "")
JARVIS_SECRET = os.environ.get("JARVIS_SECRET", "")

SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "apikey": SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def notify(msg):
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                   data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_master_prompt():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/master_prompt?is_active=eq.true&order=version.desc&limit=1", headers=SB_HEADERS)
    data = r.json()
    if data:
        print(f"[CORE] Loaded master_prompt v{data[0]['version']}")
        return data[0]["content"]
    return ""

def check_patterns(domain, task_type):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/patterns?domain=eq.{domain}&task_type=eq.{task_type}&order=quality_score.desc&limit=1", headers=SB_HEADERS)
    data = r.json()
    return data[0] if data else None

def get_mistakes(domain):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/mistakes?domain=eq.{domain}", headers=SB_HEADERS)
    return r.json()

def store_pattern(domain, task_type, agents, score, services, notes):
    httpx.post(f"{SUPABASE_URL}/rest/v1/patterns", headers=SB_HEADERS,
               json={"domain": domain, "task_type": task_type, "agent_sequence": agents,
                     "quality_score": score, "services_used": services, "notes": notes, "execution_time": 0})

def store_learning(summary, pattern, mistake, improvement):
    httpx.post(f"{SUPABASE_URL}/rest/v1/session_learning", headers=SB_HEADERS,
               json={"task_summary": summary, "new_pattern": pattern,
                     "mistake_to_avoid": mistake, "estimated_improvement": improvement})

def evolve_master_prompt(current_content, task_summary, score):
    if score >= 90:
        improvement = f"\n\n[AUTO-LEARNED {datetime.now().strftime('%Y-%m-%d')}]: Handled: {task_summary}"
        new_content = current_content + improvement
        httpx.patch(f"{SUPABASE_URL}/rest/v1/master_prompt?is_active=eq.true", headers=SB_HEADERS, json={"is_active": False})
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/master_prompt?order=version.desc&limit=1", headers=SB_HEADERS)
        data = r.json()
        next_v = (data[0]["version"] + 1) if data else 3
        httpx.post(f"{SUPABASE_URL}/rest/v1/master_prompt", headers=SB_HEADERS,
                   json={"version": next_v, "content": new_content,
                         "change_reason": f"Auto-evolved: {task_summary[:80]}", "quality_score": score, "is_active": True})
        print(f"[CORE] Master prompt evolved to v{next_v}")

def call_agent(system_prompt, task, context=""):
    msgs = [{"role": "user", "content": f"{task}\n\nContext:\n{context}" if context else task}]
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   json={"model": "claude-sonnet-4-5", "max_tokens": 4096, "system": system_prompt, "messages": msgs}, timeout=60)
    data = r.json()
    return data["content"][0]["text"] if data.get("content") else ""

ORCHESTRATOR_PROMPT = 'You are CORE orchestrator. Analyze the task and output ONLY valid JSON: {"domain":"software","task_type":"web_app","agents":[{"role":"researcher","task":"research X"},{"role":"engineer","task":"build Y"}],"services":["supabase"]}'
CRITIC_PROMPT = 'You are CORE critic. Score this output 0-100. Output ONLY valid JSON: {"score":85,"issues":[],"verdict":"approved","improvement":""}'
AGENT_PROMPTS = {
    "researcher": "You are a world-class researcher. Be comprehensive and specific.",
    "planner": "You are a master project planner. Create clear phases and milestones.",
    "engineer": "You are a senior software engineer. Write clean production-ready code.",
    "designer": "You are a UI/UX designer. Create detailed design specifications.",
    "writer": "You are a professional technical writer. Produce clear structured documents.",
    "analyst": "You are a data analyst. Provide accurate calculations and breakdowns.",
    "qa": "You are a QA engineer. Review for quality, completeness, and edge cases."
}

def execute_task(user_task):
    print(f"\n[CORE] Task: {user_task}")
    master_prompt = load_master_prompt()
    start = datetime.now()

    plan_raw = call_agent(ORCHESTRATOR_PROMPT, user_task)
    try:
        plan = json.loads(plan_raw)
    except:
        plan = {"domain": "general", "task_type": "unknown", "agents": [{"role": "writer", "task": user_task}], "services": []}

    domain = plan.get("domain", "general")
    task_type = plan.get("task_type", "unknown")
    agents = plan.get("agents", [])
    mistakes = get_mistakes(domain)
    mistakes_text = "\n".join([f"- {m['what_failed']}: {m['how_to_avoid']}" for m in mistakes]) if mistakes else "None"
    context = f"Task: {user_task}\nMistakes to avoid:\n{mistakes_text}"
    results = {}

    for agent_def in agents:
        role = agent_def.get("role", "writer")
        agent_task = agent_def.get("task", user_task)
        print(f"  -> {role} agent running...")
        result = call_agent(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]), agent_task, context)
        results[role] = result
        context += f"\n\n{role.upper()} OUTPUT:\n{result}"

    critic_raw = call_agent(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}")
    try:
        critic = json.loads(critic_raw)
        score = critic.get("score", 75)
    except:
        score = 75
        critic = {"score": 75, "issues": []}

    attempts = 1
    while score < 85 and attempts < 3:
        print(f"[CORE] Score {score}<85, improving (attempt {attempts+1})")
        role = agents[-1]["role"] if agents else "writer"
        retry = call_agent(AGENT_PROMPTS.get(role, AGENT_PROMPTS["writer"]),
                           f"Fix these issues: {critic.get('issues',[])}. Task: {user_task}", context)
        context += f"\n\nIMPROVED:\n{retry}"
        results[f"{role}_improved"] = retry
        try:
            critic = json.loads(call_agent(CRITIC_PROMPT, f"Task: {user_task}\n\nOutput:\n{context[:3000]}"))
            score = critic.get("score", 75)
        except:
            score = 75
        attempts += 1

    store_pattern(domain, task_type, [a["role"] for a in agents], score, plan.get("services", []), user_task[:100])
    store_learning(user_task[:200], str([a["role"] for a in agents]), mistakes_text[:200], min(score/100, 1.0))
    evolve_master_prompt(master_prompt, user_task[:100], score)

    duration = (datetime.now() - start).seconds
    notify(f"OK CORE: Task complete in {duration}s | Score: {score}/100 | {user_task[:60]}")

    print(f"\n{'='*50}\nFINAL OUTPUT\n{'='*50}")
    for role, output in results.items():
        print(f"\n[{role.upper()}]\n{output}\n")
    print(f"\nEXECUTION SUMMARY\nTask: {user_task[:60]}\nAgents: {[a['role'] for a in agents]}\nScore: {score}/100\nDuration: {duration}s\n")
    return context

def poll_queue():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/task_queue?status=eq.pending&order=priority.asc&limit=1", headers=SB_HEADERS)
    tasks = r.json()
    if not tasks:
        return
    task = tasks[0]
    httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB_HEADERS, json={"status": "running"})
    try:
        result = execute_task(task["task"])
        httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB_HEADERS, json={"status": "done", "result": result[:5000]})
    except Exception as e:
        httpx.patch(f"{SUPABASE_URL}/rest/v1/task_queue?id=eq.{task['id']}", headers=SB_HEADERS, json={"status": "failed", "error": str(e)})
        notify(f"FAILED CORE: {str(e)[:100]}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        execute_task(" ".join(sys.argv[1:]))
    else:
        print("[CORE] Queue polling mode started")
        notify("OK CORE: Orchestrator online - polling task queue")
        while True:
            try:
                poll_queue()
            except Exception as e:
                print(f"[CORE] Poll error: {e}")
            time.sleep(30)
