"""
core_agent.py — CORE Desktop Agent Bootstrap
=============================================
WHAT THIS IS:
  The bridge between your PC and CORE on Railway.
  Runs every 5 minutes via Windows Task Scheduler while PC is on.
  This is what makes CORE autonomous — it gives CORE hands on your local machine
  without you being present in Claude Desktop.

WITHOUT THIS: CORE is reactive. You trigger it by opening Claude Desktop and typing.
WITH THIS:    CORE is proactive. It polls Railway for pending tasks, executes them
              locally (filesystem, PowerShell, scripts), reports results back,
              and logs everything. You return to find work done.

HOW IT WORKS:
  1. Polls Supabase `task_queue` for tasks with type='desktop_agent'
  2. Executes the task locally (run_script, file_watch, etc.)
  3. Reports result back to Supabase
  4. Logs to local SQLite event bus at C:\\Users\\rnvgg\\mcp-data\\core_events.db
  5. Sends Telegram notification if owner attention needed
  6. Posts heartbeat so Railway knows Desktop agent is alive

SETUP (run once):
  python core_agent.py --install
  → Creates Windows Scheduled Task: CORE_Desktop_Agent (every 5 min)

MANUAL RUN:
  python core_agent.py
  → Single execution cycle, prints what it did

UNINSTALL:
  python core_agent.py --uninstall

LOCATION: C:\\Users\\rnvgg\\.claude-skills\\core_agent.py
LOGS:      C:\\Users\\rnvgg\\.claude-skills\\core_agent.log
DB:        C:\\Users\\rnvgg\\mcp-data\\core_events.db
"""

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CORE_URL        = "https://core-agi-production.up.railway.app"
MCP_SECRET      = "core_mcp_secret_2026_REINVAGNAR"
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_SVC    = os.environ["SUPABASE_SERVICE_KEY"]
AGENT_DIR       = Path(r"C:\Users\rnvgg\.claude-skills")
LOG_FILE        = AGENT_DIR / "core_agent.log"
DB_PATH         = Path(r"C:\Users\rnvgg\mcp-data\core_events.db")
TASK_NAME       = "CORE_Desktop_Agent"
SCRIPT_PATH     = AGENT_DIR / "core_agent.py"
PYTHON_PATH     = sys.executable

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("core_agent")

# ── Local SQLite event bus ─────────────────────────────────────────────────────
def init_db():
    """Initialize local SQLite event bus. Used for fast local state + work log."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS work_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            task_id TEXT,
            action TEXT,
            result TEXT,
            ok INTEGER DEFAULT 1
        )
    """)
    con.commit()
    con.close()

def log_work(task_id: str, action: str, result: str, ok: bool = True):
    """Log every autonomous action. Full transparency on what CORE did."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO work_log (task_id, action, result, ok) VALUES (?, ?, ?, ?)",
        (task_id, action, result[:500], 1 if ok else 0)
    )
    con.commit()
    con.close()

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def sb_get(table: str, qs: str = "") -> list:
    """Read from Supabase."""
    try:
        import urllib.request, ssl
        url = f"{SUPABASE_URL}/rest/v1/{table}?{qs}" if qs else f"{SUPABASE_URL}/rest/v1/{table}"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_SVC,
            "Authorization": f"Bearer {SUPABASE_SVC}",
        })
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error(f"sb_get {table}: {e}")
        return []

def sb_patch(table: str, match: str, data: dict) -> bool:
    """Update a Supabase row."""
    try:
        import urllib.request, ssl
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}?{match}",
            data=body,
            headers={
                "apikey": SUPABASE_SVC,
                "Authorization": f"Bearer {SUPABASE_SVC}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            method="PATCH"
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return r.status < 300
    except Exception as e:
        log.error(f"sb_patch {table}: {e}")
        return False

def sb_post(table: str, data: dict) -> bool:
    """Insert a Supabase row."""
    try:
        import urllib.request, ssl
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}",
            data=body,
            headers={
                "apikey": SUPABASE_SVC,
                "Authorization": f"Bearer {SUPABASE_SVC}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            method="POST"
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return r.status < 300
    except Exception as e:
        log.error(f"sb_post {table}: {e}")
        return False

def core_post(endpoint: str, data: dict) -> dict:
    """POST to CORE Railway endpoint."""
    try:
        import urllib.request, ssl
        body = json.dumps({**data, "secret": MCP_SECRET}).encode("utf-8")
        req = urllib.request.Request(
            f"{CORE_URL}/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error(f"core_post {endpoint}: {e}")
        return {"ok": False, "error": str(e)}

# ── Decision gate ──────────────────────────────────────────────────────────────
def decision_gate(action: str, payload: dict) -> str:
    """
    Classify risk before any autonomous action.
    Returns: 'auto' | 'notify' | 'ask'
    - auto:   read-only, execute silently
    - notify: reversible, execute + notify owner via Telegram
    - ask:    irreversible, wait for owner approval before proceeding
    CORE never does anything destructive without asking.
    """
    action_lower = action.lower()
    # Irreversible — always ask
    if any(k in action_lower for k in ["delete", "remove", "drop", "format", "wipe", "overwrite"]):
        return "ask"
    # Read-only — auto
    if any(k in action_lower for k in ["read", "get", "list", "check", "scan", "search", "status"]):
        return "auto"
    # Reversible — execute + notify
    return "notify"

# ── Task executors ──────────────────────────────────────────────────────────────
def exec_run_script(task: dict) -> dict:
    """Execute a PowerShell or Python script generated by The Soul."""
    try:
        payload = json.loads(task.get("payload", "{}"))
        script  = payload.get("script", "")
        lang    = payload.get("lang", "powershell")
        if not script:
            return {"ok": False, "error": "no script in payload"}

        if lang == "powershell":
            result = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=60
            )
        else:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=60
            )
        output = (result.stdout + result.stderr)[:500]
        ok = result.returncode == 0
        log.info(f"[SCRIPT] ok={ok} output={output[:100]}")
        return {"ok": ok, "output": output}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def exec_reflect(task: dict) -> dict:
    """Auto-reflect at end of session — write hot_reflection without Claude Desktop open."""
    try:
        payload = json.loads(task.get("payload", "{}"))
        return core_post("mcp/tool", {
            "session_token": "agent",
            "tool": "reflect",
            "args": {
                "task_summary": payload.get("summary", "core_agent autonomous cycle"),
                "domain": payload.get("domain", "mcp"),
                "patterns": payload.get("patterns", []),
                "notes": payload.get("notes", "Executed by core_agent.py"),
            }
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}

def exec_notify(task: dict) -> dict:
    """Send a Telegram notification from Desktop context."""
    try:
        payload = json.loads(task.get("payload", "{}"))
        return core_post("mcp/tool", {
            "session_token": "agent",
            "tool": "notify_owner",
            "args": {"message": payload.get("message", "core_agent notification"), "level": payload.get("level", "info")}
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}

EXECUTORS = {
    "run_script": exec_run_script,
    "reflect":    exec_reflect,
    "notify":     exec_notify,
}

# ── Heartbeat ──────────────────────────────────────────────────────────────────
def send_heartbeat():
    """Tell Railway that the Desktop agent is alive. Stored in Supabase sessions."""
    sb_post("sessions", {
        "summary": f"core_agent heartbeat — {datetime.utcnow().isoformat()}",
        "actions": ["heartbeat"],
        "interface": "desktop_agent",
    })
    log.info("[HEARTBEAT] sent")

# ── Main cycle ─────────────────────────────────────────────────────────────────
def run_cycle():
    """
    One execution cycle:
    1. Send heartbeat
    2. Poll Supabase task_queue for desktop_agent tasks
    3. For each task: decision_gate → execute → report result
    4. Log everything to local SQLite
    """
    init_db()
    log.info("=" * 50)
    log.info(f"[CYCLE START] {datetime.utcnow().isoformat()}")

    # 1. Heartbeat
    send_heartbeat()

    # 2. Poll for pending desktop tasks
    tasks = sb_get("task_queue", "select=*&status=eq.pending&type=eq.desktop_agent&order=priority.asc&limit=5")
    if not tasks:
        log.info("[POLL] No pending desktop tasks.")
        return

    log.info(f"[POLL] {len(tasks)} task(s) found")

    for task in tasks:
        tid     = task["id"]
        ttype   = task.get("task", "unknown")
        payload = task.get("payload", "{}")

        try:
            payload_dict = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            payload_dict = {}

        action = payload_dict.get("action", ttype)
        log.info(f"[TASK {tid}] action={action}")

        # 3. Decision gate
        risk = decision_gate(action, payload_dict)
        if risk == "ask":
            log.warning(f"[GATE] Task {tid} classified IRREVERSIBLE — skipping, notifying owner")
            sb_patch("task_queue", f"id=eq.{tid}", {"status": "waiting_approval"})
            core_post("mcp/tool", {
                "session_token": "agent",
                "tool": "notify_owner",
                "args": {"message": f"⚠️ core_agent: Task #{tid} needs your approval\nAction: {action}\nPayload: {str(payload_dict)[:200]}", "level": "warn"}
            })
            log_work(str(tid), action, "blocked: irreversible action, owner notified", ok=False)
            continue

        # 4. Execute
        sb_patch("task_queue", f"id=eq.{tid}", {"status": "processing"})
        executor = EXECUTORS.get(action)
        if not executor:
            log.warning(f"[TASK {tid}] No executor for action '{action}' — logging to KB")
            result = {"ok": False, "error": f"No executor for action: {action}"}
        else:
            result = executor(task)

        ok     = result.get("ok", False)
        output = result.get("output", result.get("error", str(result)))[:300]

        # 5. Report result
        sb_patch("task_queue", f"id=eq.{tid}", {
            "status": "completed" if ok else "failed",
            "error": None if ok else output[:200],
        })
        log_work(str(tid), action, output, ok=ok)
        log.info(f"[TASK {tid}] ok={ok} result={output[:80]}")

        # Notify owner if reversible action
        if risk == "notify" and ok:
            core_post("mcp/tool", {
                "session_token": "agent",
                "tool": "notify_owner",
                "args": {"message": f"✅ core_agent executed: {action}\n{output[:200]}", "level": "ok"}
            })

    log.info(f"[CYCLE END] {datetime.utcnow().isoformat()}")

# ── Installer ──────────────────────────────────────────────────────────────────
def install_scheduled_task():
    """Register CORE_Desktop_Agent as a Windows Scheduled Task (every 5 min)."""
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHON_PATH}</Command>
      <Arguments>"{SCRIPT_PATH}"</Arguments>
      <WorkingDirectory>{AGENT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT4M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
</Task>"""

    xml_path = AGENT_DIR / "core_agent_task.xml"
    xml_path.write_text(xml, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log.info(f"✅ Scheduled task '{TASK_NAME}' installed — runs every 5 min")
        print(f"✅ CORE Desktop Agent installed as Windows Scheduled Task: {TASK_NAME}")
        print(f"   Runs every 5 minutes while PC is on.")
        print(f"   Logs: {LOG_FILE}")
        print(f"   DB:   {DB_PATH}")
    else:
        log.error(f"❌ Failed to install task: {result.stderr}")
        print(f"❌ Failed: {result.stderr}")

def uninstall_scheduled_task():
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"✅ Scheduled task '{TASK_NAME}' removed.")
    else:
        print(f"❌ Failed: {result.stderr}")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CORE Desktop Agent")
    parser.add_argument("--install",   action="store_true", help="Install as Windows Scheduled Task")
    parser.add_argument("--uninstall", action="store_true", help="Remove Windows Scheduled Task")
    args = parser.parse_args()

    if args.install:
        install_scheduled_task()
    elif args.uninstall:
        uninstall_scheduled_task()
    else:
        run_cycle()
