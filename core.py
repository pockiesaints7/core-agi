"""CORE v5.0 - Single Process | Owner: REINVAGNAR | Groq only LLM"""
import base64,hashlib,json,os,threading,time
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from typing import Any,Optional
import httpx
from fastapi import FastAPI,HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

GROQ_API_KEY=os.environ["GROQ_API_KEY"]
GROQ_MODEL=os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile")
GROQ_FAST=os.environ.get("GROQ_MODEL_FAST","llama-3.1-8b-instant")
SUPABASE_URL=os.environ["SUPABASE_URL"]
SUPABASE_SVC=os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON=os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT=os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT=os.environ["GITHUB_PAT"]
GITHUB_REPO=os.environ.get("GITHUB_USERNAME","pockiesaints7")+"/core-agi"
MCP_SECRET=os.environ["MCP_SECRET"]
PORT=int(os.environ.get("PORT",8080))
SESSION_TTL_H=8

def call_groq(system,user,model=None,max_tokens=2048):
    try:
        r=httpx.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":model or GROQ_MODEL,"max_tokens":max_tokens,
                  "messages":[{"role":"system","content":system},{"role":"user","content":user}]},
            timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e: print(f"[GROQ] {e}"); return ""

def call_groq_fast(system,user,max_tokens=512):
    return call_groq(system,user,model=GROQ_FAST,max_tokens=max_tokens)

class RateLimiter:
    def __init__(self):
        self.calls=defaultdict(list)
        try: self.c=json.load(open("resource_ceilings.json"))
        except: self.c={"groq_calls_per_hour":200,"supabase_writes_per_hour":500,"github_pushes_per_hour":20,"telegram_messages_per_hour":30,"mcp_tool_calls_per_minute":30}
    def _ok(self,key,window,limit):
        now=time.time(); self.calls[key]=[t for t in self.calls[key] if now-t<window]
        if len(self.calls[key])>=limit: return False
        self.calls[key].append(now); return True
    def groq(self): return self._ok("groq",3600,self.c.get("groq_calls_per_hour",200))
    def tg(self): return self._ok("tg",3600,self.c.get("telegram_messages_per_hour",30))
    def gh(self): return self._ok("gh",3600,self.c.get("github_pushes_per_hour",20))
    def sbw(self): return self._ok("sbw",3600,self.c.get("supabase_writes_per_hour",500))
    def mcp(self,sid): return self._ok(f"mcp:{sid}",60,self.c.get("mcp_tool_calls_per_minute",30))
L=RateLimiter()


def _sbh(svc=False):
    k=SUPABASE_SVC if svc else SUPABASE_ANON
    return {"apikey":k,"Authorization":f"Bearer {k}","Content-Type":"application/json","Prefer":"return=minimal"}
def sb_get(t,qs=""):
    r=httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}",headers=_sbh(),timeout=15); r.raise_for_status(); return r.json()
def sb_post(t,d):
    if not L.sbw(): return False
    return httpx.post(f"{SUPABASE_URL}/rest/v1/{t}",headers=_sbh(True),json=d,timeout=15).is_success
def sb_patch(t,m,d):
    if not L.sbw(): return False
    return httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}",headers=_sbh(True),json=d,timeout=15).is_success

def notify(msg,cid=None):
    if not L.tg(): return False
    try: httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",data={"chat_id":cid or TELEGRAM_CHAT,"text":msg[:4000],"parse_mode":"Markdown"},timeout=10); return True
    except Exception as e: print(f"[TG] {e}"); return False
def set_webhook():
    d=os.environ.get("RAILWAY_PUBLIC_DOMAIN","")
    if not d: return
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",data={"url":f"https://{d}/webhook"})
    print("[CORE] Webhook set")

def _ghh(): return {"Authorization":f"Bearer {GITHUB_PAT}","Accept":"application/vnd.github.v3+json"}
def gh_read(path,repo=None):
    r=httpx.get(f"https://api.github.com/repos/{repo or GITHUB_REPO}/contents/{path}",headers=_ghh(),timeout=15)
    r.raise_for_status(); return base64.b64decode(r.json()["content"]).decode()
def gh_write(path,content,msg,repo=None):
    if not L.gh(): return False
    repo=repo or GITHUB_REPO; h=_ghh(); sha=None
    try: sha=httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}",headers=h,timeout=10).json().get("sha")
    except: pass
    p={"message":msg,"content":base64.b64encode(content.encode()).decode()}
    if sha: p["sha"]=sha
    return httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}",headers=h,json=p,timeout=20).is_success

def load_master_prompt():
    d=sb_get("master_prompt","is_active=eq.true&order=version.desc&limit=1")
    return (d[0]["content"],d[0]["version"]) if d else ("",0)
def get_agi_status():
    d=sb_get("agi_status","limit=1"); return d[0] if d else {}
def get_context(domain,kw=""):
    ctx=sb_get("agi_context",f"domain=eq.{domain}&limit=10")
    if kw:
        extra=sb_get("agi_context",f"key=ilike.*{kw.split()[0]}*&limit=5")
        seen={str(x) for x in ctx}; ctx+=[x for x in extra if str(x) not in seen]
    return ctx[:12]
def get_mistakes(domain): return sb_get("agi_mistakes",f"domain=eq.{domain}&limit=10")
def get_playbook(kw):
    if not kw: return []
    return sb_get("playbook",f"topic=ilike.*{kw.split()[0]}*&limit=3")
def store_knowledge(domain,topic,content,tags,conf="learned"):
    sb_post("knowledge_base",{"domain":domain,"topic":topic,"content":content,"source":"core_agi","confidence":conf,"tags":tags})
def store_mistake(ctx,wf,ca,tags,domain="general"):
    sb_post("mistakes",{"context":ctx,"what_failed":wf,"correct_approach":ca,"domain":domain,"tags":tags,"severity":"medium"})
def store_pattern(domain,tt,agents,score,svcs,notes):
    sb_post("patterns",{"domain":domain,"task_type":tt,"agent_sequence":agents,"quality_score":score,"services_used":svcs,"notes":notes,"execution_time":0})
def log_session(summary,actions,iface="telegram"):
    sb_post("sessions",{"summary":summary,"actions":actions,"interface":iface})
def update_master_prompt(content,reason,score=90):
    sb_patch("master_prompt","is_active=eq.true",{"is_active":False})
    d=sb_get("master_prompt","order=version.desc&limit=1")
    v=(d[0]["version"]+1) if d else 6
    sb_post("master_prompt",{"version":v,"content":content,"change_reason":reason,"quality_score":score,"is_active":True})
    return v


AGENTS={"researcher":"You are a world-class researcher. Be comprehensive. State what you know vs uncertain.",
"planner":"You are a master project planner. Clear phases, milestones, dependencies, risk mitigations.",
"engineer":"You are a senior software engineer. Write clean, production-ready, well-commented code.",
"designer":"You are a UI/UX designer. Create detailed design specs and user flows.",
"writer":"You are a professional technical writer. Produce clear, well-structured documents.",
"analyst":"You are a data analyst. Provide accurate calculations, assumptions, and breakdowns.",
"qa":"You are a QA engineer. Review for quality, completeness, edge cases, and failures."}
ORCH_SYS="Analyze the task. Output ONLY valid JSON: {\"domain\":\"software\",\"task_type\":\"web_app\",\"agents\":[{\"role\":\"researcher\",\"task\":\"research X\"}],\"services\":[\"supabase\"]}"
CRITIC_SYS="Score 0-100. Output ONLY valid JSON: {\"score\":85,\"issues\":[],\"verdict\":\"approved\"}"
EVOLVER_SYS="You are CORE prompt engineer. Return improved master prompt OR exactly: NO_CHANGE. Be conservative."
EXTRACT_SYS="Extract reusable knowledge. Output ONLY valid JSON: {\"new_knowledge\":[{\"domain\":\"X\",\"topic\":\"Y\",\"content\":\"Z\",\"tags\":[\"a\"]}],\"new_mistakes\":[]}"

def execute_task(task,cid=None):
    cid=cid or TELEGRAM_CHAT
    notify(f"CORE: Starting...\n`{task[:80]}`",cid)
    master,_=load_master_prompt(); start=datetime.now(timezone.utc)
    if not L.groq(): notify("Groq ceiling hit.",cid); return ""
    try: plan=json.loads(call_groq(ORCH_SYS,task,max_tokens=512))
    except: plan={"domain":"general","task_type":"unknown","agents":[{"role":"writer","task":task}],"services":[]}
    domain=plan.get("domain","general"); agents=plan.get("agents",[{"role":"writer","task":task}])
    kb=get_context(domain,task); mk=get_mistakes(domain); pb=get_playbook(domain)
    ctx=(f"Task: {task}\n\nKNOWLEDGE ({len(kb)}):\n"+"\n".join([f"- {x.get(\"key\",\"\")}: {str(x.get(\"value\",\"\"))[:150]}" for x in kb])+
         "\n\nMETHODS:\n"+"\n".join([f"- {p.get(\"topic\",\"\")}: {p.get(\"method\",\"\")[:120]}" for p in pb])+
         "\n\nAVOID:\n"+"\n".join([f"- {m.get(\"what_failed\",\"\")[:80]}" for m in mk]))
    results={}
    for a in agents:
        role=a.get("role","writer"); t=a.get("task",task)
        if not L.groq(): break
        out=call_groq(AGENTS.get(role,AGENTS["writer"]),t+"\n\nContext:\n"+ctx)
        results[role]=out; ctx+=f"\n\n{role.upper()}:\n{out}"
    try: critic=json.loads(call_groq_fast(CRITIC_SYS,f"Task:{task}\n\nOutput:\n{ctx[:3000]}")); score=critic.get("score",75)
    except: score,critic=75,{"score":75,"issues":[]}
    attempts=1
    while score<85 and attempts<3:
        if not L.groq(): break
        role=agents[-1].get("role","writer") if agents else "writer"
        retry=call_groq(AGENTS.get(role,AGENTS["writer"]),f"Fix:{critic.get(\"issues\",[])}\nTask:{task}\n\nCtx:\n{ctx}")
        ctx+=f"\n\nIMPROVED:\n{retry}"
        try: critic=json.loads(call_groq_fast(CRITIC_SYS,f"Task:{task}\n\nOutput:\n{ctx[:3000]}")); score=critic.get("score",75)
        except: score=75
        attempts+=1
    store_pattern(domain,plan.get("task_type","?"),[a["role"] for a in agents],score,plan.get("services",[]),task[:100])
    log_session(f"Task: {task[:100]}",[a["role"] for a in agents])
    if L.groq():
        try:
            ex=json.loads(call_groq(EXTRACT_SYS,f"Task:{task}\n\nOutput:\n{ctx[:2000]}",max_tokens=1024))
            for k in ex.get("new_knowledge",[]): store_knowledge(k["domain"],k["topic"],k["content"],k.get("tags",[]))
            for m in ex.get("new_mistakes",[]): store_mistake(task[:100],m.get("what",""),m.get("avoid",""),[domain])
        except Exception as e: print(f"[CORE] Extract: {e}")
    if L.groq():
        ev=call_groq(EVOLVER_SYS,f"Prompt:\n{master[:800]}\n\nTask:{task}\nScore:{score}")
        if ev.strip()!="NO_CHANGE" and len(ev)>100:
            v=update_master_prompt(ev,f"Auto-evolved:{task[:60]}",score)
            gh_write("master_prompt.md",ev,f"Auto-sync v{v}")
            notify(f"Prompt evolved to v{v}",cid)
    dur=(datetime.now(timezone.utc)-start).seconds
    prev=list(results.values())[0][:400] if results else ""
    notify(f"CORE Done\nTask: {task[:60]}\nScore: {score}/100\nAgents: {str([a[\"role\"] for a in agents])}\nTime: {dur}s\n\n{prev}...",cid)
    return ctx


TRAIN_SYS="You are CORE internal trainer. Generate and solve a self-improvement task. Output ONLY valid JSON: {\"task\":\"\",\"domain\":\"\",\"reasoning\":\"\",\"output\":\"\",\"score\":85,\"new_knowledge\":[{\"domain\":\"X\",\"topic\":\"Y\",\"content\":\"Z\",\"tags\":[\"a\"]}],\"new_mistakes\":[{\"what\":\"\",\"avoid\":\"\"}],\"gap_found\":\"\"}"
AUDIT_SYS="You are CORE self-auditor. Output ONLY valid JSON: {\"gap\":\"\",\"fix\":\"\",\"priority\":\"high|medium|low\"}"
_cycles=0; _last_train=None; _train_on=True

def training_loop():
    global _cycles,_last_train
    print("[TRAINING] Started"); time.sleep(30)
    while _train_on:
        try:
            if not L.groq(): time.sleep(300); continue
            master,_=load_master_prompt(); s=get_agi_status()
            raw=call_groq(TRAIN_SYS,f"Prompt:\n{master[:500]}\nSystem:{json.dumps(s)}\nCycle #{_cycles+1}",max_tokens=1500)
            if not raw: time.sleep(60); continue
            res=json.loads(raw)
            for k in res.get("new_knowledge",[]): store_knowledge(k.get("domain","training"),k.get("topic",""),k.get("content",""),k.get("tags",[]),"training")
            for m in res.get("new_mistakes",[]): store_mistake("training",m.get("what",""),m.get("avoid",""),["training"])
            if L.groq() and res.get("gap_found"):
                try:
                    audit=json.loads(call_groq_fast(AUDIT_SYS,json.dumps(res)))
                    if audit.get("priority") in ("high","medium"):
                        sb_post("memory",{"category":"training_gap","key":f"gap_{_cycles}","value":json.dumps(audit)})
                except: pass
            _cycles+=1; _last_train=datetime.now(timezone.utc).isoformat()
            print(f"[TRAINING] Cycle {_cycles} score={res.get(\"score\",\"?\")} domain={res.get(\"domain\",\"?\")}")
            time.sleep(45)
        except Exception as e: print(f"[TRAINING] {e}"); time.sleep(60)

def queue_poller():
    print("[QUEUE] Started")
    while True:
        try:
            tasks=sb_get("task_queue","status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t=tasks[0]; sb_patch("task_queue",f"id=eq.{t[\"id\"]}",{"status":"running"})
                try:
                    res=execute_task(t["task"],t.get("chat_id"))
                    sb_patch("task_queue",f"id=eq.{t[\"id\"]}",{"status":"done","result":res[:5000]})
                except Exception as e:
                    sb_patch("task_queue",f"id=eq.{t[\"id\"]}",{"status":"failed","error":str(e)})
                    notify(f"Queue failed: {str(e)[:100]}")
        except Exception as e: print(f"[QUEUE] {e}")
        time.sleep(30)

_sessions={}
def mcp_new(ip):
    tok=hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[tok]={"ip":ip,"expires":(datetime.utcnow()+timedelta(hours=SESSION_TTL_H)).isoformat(),"calls":0}
    return tok
def mcp_ok(tok):
    if tok not in _sessions: return False
    if datetime.utcnow()>datetime.fromisoformat(_sessions[tok]["expires"]): del _sessions[tok]; return False
    _sessions[tok]["calls"]+=1; return True


def t_state():
    mp=sb_get("master_prompt","select=version,content&is_active=eq.true&limit=1")
    st=sb_get("agi_status","limit=1"); tq=sb_get("task_queue","select=id,task,status&status=eq.pending&limit=5")
    return {"prompt_version":mp[0]["version"] if mp else "?","prompt_preview":mp[0]["content"][:500] if mp else "",
            "system":st[0] if st else {},"pending":tq,"training_cycles":_cycles,"last_training":_last_train}
def t_health():
    h={"ts":datetime.utcnow().isoformat(),"components":{}}
    for name,fn in [("supabase",lambda:sb_get("master_prompt","select=id&limit=1")),
                    ("groq",lambda:httpx.get("https://api.groq.com/openai/v1/models",headers={"Authorization":f"Bearer {GROQ_API_KEY}"},timeout=5).raise_for_status()),
                    ("telegram",lambda:httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",timeout=5).raise_for_status()),
                    ("github",lambda:gh_read("README.md"))]:
        try: fn(); h["components"][name]="ok"
        except Exception as e: h["components"][name]=f"error:{e}"
    h["training"]={"cycles":_cycles,"last":_last_train}
    h["overall"]="ok" if all(v=="ok" for v in h["components"].values()) else "degraded"
    return h
def t_constitution():
    try:
        with open("constitution.txt") as f: txt=f.read()
    except: txt=gh_read("constitution.txt")
    return {"constitution":txt,"immutable":True}
def t_search_kb(query="",domain="",limit=10):
    qs=f"select=domain,topic,content,confidence&limit={limit}"
    if domain: qs+=f"&domain=eq.{domain}"
    if query: qs+=f"&content=ilike.*{query.split()[0]}*"
    return sb_get("knowledge_base",qs)
def t_get_mistakes(domain="general",limit=10):
    qs=f"select=context,what_failed,correct_approach&limit={limit}"
    if domain and domain!="all": qs+=f"&domain=eq.{domain}"
    return sb_get("mistakes",qs)
def t_update_state(key,value,reason): return {"ok":sb_post("memory",{"category":"mcp_state","key":key,"value":str(value),"note":reason}),"key":key}
def t_add_knowledge(domain,topic,content,tags,confidence="medium"): return {"ok":sb_post("knowledge_base",{"domain":domain,"topic":topic,"content":content,"confidence":confidence,"tags":tags,"source":"mcp_session"}),"topic":topic}
def t_log_mistake(context,what_failed,fix,domain="general"): return {"ok":sb_post("mistakes",{"domain":domain,"context":context,"what_failed":what_failed,"correct_approach":fix})}
def t_read_file(path,repo=""): 
    try: return {"ok":True,"content":gh_read(path,repo or GITHUB_REPO)[:5000]}
    except Exception as e: return {"ok":False,"error":str(e)}
def t_write_file(path,content,message,repo=""):
    ok=gh_write(path,content,message,repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{path}`")
    return {"ok":ok,"path":path}
def t_notify(message,level="info"):
    icons={"info":"i","warn":"!","alert":"ALERT","ok":"OK"}
    return {"ok":notify(f"{icons.get(level,chr(187))} CORE\n{message}")}
def t_sb_query(table,query_string="",limit=20): return sb_get(table,f"{query_string}&limit={limit}" if query_string else f"limit={limit}")
def t_sb_insert(table,data): return {"ok":sb_post(table,data),"table":table}
def t_training(): return {"active":_train_on,"cycles":_cycles,"last":_last_train,"model":GROQ_MODEL}

TOOLS={
    "get_state":          {"fn":t_state,         "perm":"READ",    "args":[]},
    "get_system_health":  {"fn":t_health,         "perm":"READ",    "args":[]},
    "get_constitution":   {"fn":t_constitution,   "perm":"READ",    "args":[]},
    "get_training_status":{"fn":t_training,       "perm":"READ",    "args":[]},
    "search_kb":          {"fn":t_search_kb,      "perm":"READ",    "args":["query","domain","limit"]},
    "get_mistakes":       {"fn":t_get_mistakes,   "perm":"READ",    "args":["domain","limit"]},
    "read_file":          {"fn":t_read_file,      "perm":"READ",    "args":["path","repo"]},
    "sb_query":           {"fn":t_sb_query,       "perm":"READ",    "args":["table","query_string","limit"]},
    "update_state":       {"fn":t_update_state,   "perm":"WRITE",   "args":["key","value","reason"]},
    "add_knowledge":      {"fn":t_add_knowledge,  "perm":"WRITE",   "args":["domain","topic","content","tags","confidence"]},
    "log_mistake":        {"fn":t_log_mistake,    "perm":"WRITE",   "args":["context","what_failed","fix","domain"]},
    "notify_owner":       {"fn":t_notify,         "perm":"WRITE",   "args":["message","level"]},
    "sb_insert":          {"fn":t_sb_insert,      "perm":"WRITE",   "args":["table","data"]},
    "write_file":         {"fn":t_write_file,     "perm":"EXECUTE", "args":["path","content","message","repo"]},
}

class Handshake(BaseModel):
    secret:str; client_id:Optional[str]="claude_desktop"
class ToolCall(BaseModel):
    session_token:str; tool:str; args:dict={}

app=FastAPI(title="CORE v5.0",version="5.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

@app.get("/")
def root():
    s=get_agi_status()
    return {"service":"CORE v5.0","prompt_v":s.get("master_prompt_version","?"),"knowledge":s.get("knowledge_entries",0),"training_cycles":_cycles}

@app.get("/health")
def health_ep(): return t_health()

@app.post("/webhook")
async def webhook(req:Request):
    try:
        u=await req.json()
        if "message" in u: threading.Thread(target=handle_msg,args=(u["message"],),daemon=True).start()
    except Exception as e: print(f"[WEBHOOK] {e}")
    return {"ok":True}

@app.post("/mcp/startup")
async def mcp_startup(body:Handshake,req:Request):
    if body.secret!=MCP_SECRET: raise HTTPException(401,"Invalid secret")
    tok=mcp_new(req.client.host)
    notify(f"MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {"session_token":tok,"expires_hours":SESSION_TTL_H,
            "state":t_state(),"health":t_health(),"constitution":t_constitution(),
            "tools":list(TOOLS.keys()),"note":"3 auto-calls complete. CORE fully aware."}

@app.post("/mcp/auth")
async def mcp_auth(body:Handshake,req:Request):
    if body.secret!=MCP_SECRET: notify(f"Invalid MCP auth from {req.client.host}"); raise HTTPException(401,"Invalid secret")
    return {"session_token":mcp_new(req.client.host),"expires_hours":SESSION_TTL_H}

@app.post("/mcp/tool")
async def mcp_tool(body:ToolCall):
    if not mcp_ok(body.session_token): raise HTTPException(401,"Invalid/expired session")
    if not L.mcp(body.session_token): raise HTTPException(429,"Rate limit exceeded")
    if body.tool not in TOOLS: raise HTTPException(404,f"Tool not found: {body.tool}")
    try:
        res=TOOLS[body.tool]["fn"](**body.args) if body.args else TOOLS[body.tool]["fn"]()
        return {"ok":True,"tool":body.tool,"perm":TOOLS[body.tool]["perm"],"result":res}
    except HTTPException: raise
    except Exception as e: return {"ok":False,"tool":body.tool,"error":str(e)}

@app.get("/mcp/tools")
def list_tools(): return {n:{"perm":t["perm"],"args":t["args"]} for n,t in TOOLS.items()}

def handle_msg(msg):
    cid=str(msg.get("chat",{}).get("id","")); text=msg.get("text","").strip()
    if not text: return
    if text=="/start":
        s=get_agi_status()
        notify(f"*CORE v5.0*\nKnowledge: {s.get(\"knowledge_entries\",0)}\nPlaybook: {s.get(\"playbook_entries\",0)}\nMistakes: {s.get(\"mistake_entries\",0)}\nTraining: {_cycles} cycles\nPrompt: v{s.get(\"master_prompt_version\",\"?\")}\n\n/status /prompt /tasks /ask <q>\nOr send any task.",cid)
    elif text=="/status":
        s=get_agi_status(); h=t_health()
        notify(f"*Status*\nSupabase: {h[\"components\"].get(\"supabase\")}\nGroq: {h[\"components\"].get(\"groq\")}\nTelegram: {h[\"components\"].get(\"telegram\")}\nGitHub: {h[\"components\"].get(\"github\")}\n\nKnowledge: {s.get(\"knowledge_entries\",0)}\nTraining: {_cycles} cycles\nPrompt: v{s.get(\"master_prompt_version\",\"?\")}",cid)
    elif text=="/prompt":
        c,v=load_master_prompt(); notify(f"*Prompt v{v}*\n\n{c[:800]}...",cid)
    elif text=="/tasks":
        t=sb_get("patterns","order=created_at.desc&limit=5")
        notify("*Recent*\n\n"+"\n".join([f"- {x.get(\"notes\",\"?\")[:50]} ({x.get(\"quality_score\",0)})" for x in t]) if t else "No tasks yet.",cid)
    elif text.startswith("/ask "):
        r=t_search_kb(text[5:].strip(),limit=5)
        notify("\n\n".join([f"*{x.get(\"topic\",\"\")}*\n{str(x.get(\"content\",\"\"))[:200]}" for x in r]) if r else "Nothing found.",cid)
    else:
        threading.Thread(target=execute_task,args=(text,cid),daemon=True).start()

@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller,daemon=True).start()
    threading.Thread(target=training_loop,daemon=True).start()
    s=get_agi_status()
    notify(f"*CORE v5.0 Online*\nKnowledge: {s.get(\"knowledge_entries\",0)}\nPrompt: v{s.get(\"master_prompt_version\",\"?\")}\nGroq: {GROQ_MODEL}\nMCP: ready on /mcp/*")
    print(f"[CORE] v5.0 online :{PORT}")

if __name__=="__main__":
    import uvicorn
    uvicorn.run("core:app",host="0.0.0.0",port=PORT,reload=False)

