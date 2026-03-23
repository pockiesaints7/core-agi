"""
core_agent.py — CORE VM Agent (Linux Edition)
==============================================
WHAT THIS IS:
  The VM equivalent of the Windows Desktop Agent.
  Runs as a systemd service 24/7 on Oracle Ubuntu VM.
  Gives CORE full autonomous control over the VM —
  filesystem, shell, sudo, git, services, python scripts.

CAPABILITIES (vs Windows version):
  + bash/shell execution (replaces PowerShell)
  + Full filesystem read/write/delete
  + sudo commands (service restart, apt, etc.)
  + Git operations (pull, push, status)
  + Python script execution
  + Service management (systemctl)
  + File watch
  + HTTP requests

SETUP (run once):
  sudo python3 core_agent.py --install
  → Creates systemd service: core-vm-agent (runs continuously)

MANUAL RUN:
  python3 core_agent.py
  → Single execution cycle

UNINSTALL:
  sudo python3 core_agent.py --uninstall

LOGS: /home/ubuntu/core-agi/core_agent.log
DB:   /home/ubuntu/core-agi/core_agent.db
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CORE_URL        = "https://core-agi.duckdns.org"
MCP_SECRET      = os.environ.get("MCP_SECRET", "core_mcp_secret_2026_REINVAGNAR")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_SVC    = os.environ.get("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
AGENT_DIR       = Path("/home/ubuntu/core-agi")
LOG_FILE        = AGENT_DIR / "core_agent.log"
DB_PATH         = AGENT_DIR / "core_agent.db"
PYTHON_PATH     = sys.executable
POLL_INTERVAL   = 10   # seconds between task queue polls
HEARTBEAT_EVERY = 60   # seconds between heartbeats

# ── Logging ───────────────────────────────────────────────────────────────────
AGENT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("core_agent")

# ── Local SQLite event bus ────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS work_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT DEFAULT (datetime('now')),
            task_id   TEXT,
            action    TEXT,
            result    TEXT,
            ok        INTEGER DEFAULT 1
        )
    """)
    con.commit()
    con.close()

def log_work(task_id: str, action: str, result: str, ok: bool = True):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO work_log (task_id, action, result, ok) VALUES (?, ?, ?, ?)",
            (task_id, action, result[:1000], 1 if ok else 0)
        )
        con.commit()
        con.close()
    except Exception as e:
        log.error(f"log_work error: {e}")

# ── Supabase helpers ──────────────────────────────────────────────────────────
def sb_get(table: str, qs: str = "") -> list:
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

# ── Telegram helper ───────────────────────────────────────────────────────────
def tg_send(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        import urllib.request, ssl, urllib.parse
        body = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT,
            "text": msg[:4000],
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=body,
        )
        ctx = ssl.create_default_context()
        urllib.request.urlopen(req, context=ctx, timeout=10)
    except Exception as e:
        log.error(f"tg_send error: {e}")

# ── Decision gate ─────────────────────────────────────────────────────────────
def decision_gate(action: str, payload: dict) -> str:
    """
    Classify risk level before executing.
    Returns: 'auto' | 'notify' | 'ask'
    - auto:   read-only ops, execute silently
    - notify: reversible ops, execute + notify owner
    - ask:    destructive ops, wait for owner approval
    """
    a = action.lower()
    cmd = payload.get("command", "") or payload.get("script", "")
    cmd_lower = str(cmd).lower()

    # Always ask for destructive operations
    destructive = ["rm -rf", "drop table", "delete from", "format", "wipe",
                   "mkfs", "dd if=", "> /dev/", "truncate"]
    if any(k in a for k in ["delete", "drop", "wipe", "format"]):
        return "ask"
    if any(k in cmd_lower for k in destructive):
        return "ask"

    # Auto for read-only
    if any(k in a for k in ["read", "get", "list", "check", "scan", "search",
                              "status", "cat", "ls", "find", "grep", "ps",
                              "df", "du", "whoami", "uname"]):
        return "auto"

    # Notify for everything else (write, execute, install, restart)
    return "notify"

# ── EXECUTORS ─────────────────────────────────────────────────────────────────

def exec_shell(task: dict) -> dict:
    """
    Execute any bash command or script on the VM.
    payload: {command: "...", timeout: 60, sudo: false}
    Most powerful executor — full shell access.
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        command = payload.get("command") or payload.get("script", "")
        timeout = int(payload.get("timeout", 60))
        use_sudo = payload.get("sudo", False)

        if not command:
            return {"ok": False, "error": "no command in payload"}

        if use_sudo and not command.strip().startswith("sudo"):
            command = f"sudo {command}"

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(AGENT_DIR),
        )
        output = (result.stdout + result.stderr).strip()[:2000]
        ok = result.returncode == 0
        log.info(f"[SHELL] ok={ok} cmd={command[:80]} output={output[:100]}")
        return {"ok": ok, "output": output, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_run_script(task: dict) -> dict:
    """
    Run a bash or python script.
    payload: {script: "...", lang: "bash"|"python", timeout: 60}
    Writes to temp file then executes.
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        script  = payload.get("script", "")
        lang    = payload.get("lang", "bash")
        timeout = int(payload.get("timeout", 60))

        if not script:
            return {"ok": False, "error": "no script in payload"}

        # Write to temp file
        ext      = ".sh" if lang == "bash" else ".py"
        tmp_path = Path(f"/tmp/core_agent_script_{int(time.time())}{ext}")
        tmp_path.write_text(script)
        tmp_path.chmod(0o755)

        if lang == "bash":
            cmd = ["bash", str(tmp_path)]
        else:
            cmd = [PYTHON_PATH, str(tmp_path)]

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(AGENT_DIR)
        )
        tmp_path.unlink(missing_ok=True)
        output = (result.stdout + result.stderr).strip()[:2000]
        ok = result.returncode == 0
        log.info(f"[SCRIPT] lang={lang} ok={ok} output={output[:100]}")
        return {"ok": ok, "output": output, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "script timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_file_read(task: dict) -> dict:
    """
    Read a file from the VM filesystem.
    payload: {path: "/path/to/file", lines: 100}  # lines=0 means full file
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        path    = payload.get("path", "")
        lines   = int(payload.get("lines", 0))

        if not path:
            return {"ok": False, "error": "no path in payload"}

        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"file not found: {path}"}

        content = p.read_text(errors="replace")
        if lines > 0:
            content = "\n".join(content.splitlines()[:lines])

        return {"ok": True, "path": path, "content": content[:10000], "size": p.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_file_write(task: dict) -> dict:
    """
    Write or append to a file on the VM filesystem.
    payload: {path: "/path/to/file", content: "...", mode: "write"|"append"}
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        path    = payload.get("path", "")
        content = payload.get("content", "")
        mode    = payload.get("mode", "write")

        if not path:
            return {"ok": False, "error": "no path in payload"}

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if mode == "append":
            with open(p, "a") as f:
                f.write(content)
        else:
            p.write_text(content)

        return {"ok": True, "path": path, "size": p.stat().st_size, "mode": mode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_file_list(task: dict) -> dict:
    """
    List files in a directory.
    payload: {path: "/path/to/dir", pattern: "*.py"}
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        path    = payload.get("path", str(AGENT_DIR))
        pattern = payload.get("pattern", "*")

        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"path not found: {path}"}

        files = [
            {
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size if f.is_file() else 0,
                "type": "file" if f.is_file() else "dir",
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            }
            for f in sorted(p.glob(pattern))
        ]
        return {"ok": True, "path": path, "count": len(files), "files": files[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_git(task: dict) -> dict:
    """
    Git operations on any repo on the VM.
    payload: {repo_path: "/home/ubuntu/core-agi", operation: "pull"|"status"|"log"|"push", message: "commit msg"}
    """
    try:
        payload   = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        repo_path = payload.get("repo_path", str(AGENT_DIR))
        operation = payload.get("operation", "status")
        message   = payload.get("message", "CORE agent auto-commit")

        ops = {
            "pull":   ["git", "pull"],
            "status": ["git", "status", "--short"],
            "log":    ["git", "log", "--oneline", "-10"],
            "push":   ["git", "push"],
            "diff":   ["git", "diff", "--stat"],
            "commit": ["git", "commit", "-am", message],
        }

        if operation not in ops:
            return {"ok": False, "error": f"unknown git operation: {operation}"}

        result = subprocess.run(
            ops[operation], capture_output=True, text=True,
            timeout=60, cwd=repo_path
        )
        output = (result.stdout + result.stderr).strip()
        return {"ok": result.returncode == 0, "operation": operation, "output": output[:2000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_service(task: dict) -> dict:
    """
    Manage systemd services.
    payload: {service: "core-agi", operation: "restart"|"stop"|"start"|"status"}
    """
    try:
        payload   = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        service   = payload.get("service", "core-agi")
        operation = payload.get("operation", "status")

        allowed_ops = ["restart", "stop", "start", "status", "reload"]
        if operation not in allowed_ops:
            return {"ok": False, "error": f"operation must be one of {allowed_ops}"}

        result = subprocess.run(
            ["sudo", "systemctl", operation, service],
            capture_output=True, text=True, timeout=30
        )
        output = (result.stdout + result.stderr).strip()
        return {"ok": result.returncode == 0, "service": service, "operation": operation, "output": output[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_install_package(task: dict) -> dict:
    """
    Install a package via apt or pip.
    payload: {package: "htop", manager: "apt"|"pip"}
    """
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        package = payload.get("package", "")
        manager = payload.get("manager", "pip")

        if not package:
            return {"ok": False, "error": "no package specified"}

        if manager == "apt":
            cmd = ["sudo", "apt", "install", "-y", package]
        else:
            cmd = [PYTHON_PATH, "-m", "pip", "install", package, "--break-system-packages"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout + result.stderr).strip()[-1000:]
        return {"ok": result.returncode == 0, "package": package, "manager": manager, "output": output}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_http_request(task: dict) -> dict:
    """
    Make an HTTP request from the VM.
    payload: {url: "...", method: "GET"|"POST", headers: {}, body: {}}
    Useful for calling APIs, webhooks, checking URLs.
    """
    try:
        import urllib.request, ssl, urllib.parse
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        url     = payload.get("url", "")
        method  = payload.get("method", "GET").upper()
        headers = payload.get("headers", {})
        body    = payload.get("body")

        if not url:
            return {"ok": False, "error": "no url in payload"}

        data = json.dumps(body).encode() if body else None
        if data and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            response_body = r.read().decode(errors="replace")[:5000]
            return {"ok": True, "status": r.status, "body": response_body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_reflect(task: dict) -> dict:
    """Post a reflection to CORE via MCP tool."""
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        return exec_http_request({"payload": json.dumps({
            "url": f"{CORE_URL}/mcp/tool",
            "method": "POST",
            "body": {
                "session_token": "agent",
                "tool": "reflect",
                "args": {
                    "task_summary": payload.get("summary", "core_agent VM cycle"),
                    "domain": payload.get("domain", "vm"),
                    "patterns": payload.get("patterns", []),
                    "notes": payload.get("notes", "Executed by core_agent.py on VM"),
                }
            }
        })})
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_notify(task: dict) -> dict:
    """Send Telegram notification."""
    try:
        payload = json.loads(task.get("payload", "{}")) if isinstance(task.get("payload"), str) else task.get("payload", {})
        msg = payload.get("message", "core_agent notification")
        tg_send(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def exec_vm_info(task: dict) -> dict:
    """Return VM system info — disk, memory, CPU, uptime, running services."""
    try:
        result = subprocess.run(
            "echo '=== DISK ===' && df -h / && "
            "echo '=== MEMORY ===' && free -h && "
            "echo '=== CPU ===' && top -bn1 | grep 'Cpu(s)' && "
            "echo '=== UPTIME ===' && uptime && "
            "echo '=== SERVICES ===' && systemctl list-units --type=service --state=running --no-pager | head -20",
            shell=True, capture_output=True, text=True, timeout=15
        )
        return {"ok": True, "info": result.stdout[:3000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Executor registry ─────────────────────────────────────────────────────────
EXECUTORS = {
    "shell":           exec_shell,
    "run_script":      exec_run_script,
    "file_read":       exec_file_read,
    "file_write":      exec_file_write,
    "file_list":       exec_file_list,
    "git":             exec_git,
    "service":         exec_service,
    "install_package": exec_install_package,
    "http_request":    exec_http_request,
    "reflect":         exec_reflect,
    "notify":          exec_notify,
    "vm_info":         exec_vm_info,
}

# ── Heartbeat ─────────────────────────────────────────────────────────────────
def send_heartbeat():
    sb_post("sessions", {
        "summary": f"core_agent VM heartbeat — {datetime.utcnow().isoformat()}",
        "actions": ["heartbeat"],
        "interface": "vm_agent",
    })
    log.info("[HEARTBEAT] sent")

# ── Main loop ─────────────────────────────────────────────────────────────────
def run_loop():
    """
    Continuous polling loop:
    1. Heartbeat every 60s
    2. Poll task_queue for vm_agent tasks every 10s
    3. For each task: decision_gate → execute → report
    """
    init_db()
    log.info("=" * 60)
    log.info(f"[CORE VM AGENT] Starting — {datetime.utcnow().isoformat()}")
    log.info(f"[CORE VM AGENT] Executors: {list(EXECUTORS.keys())}")
    log.info("=" * 60)
    tg_send("🖥️ <b>CORE VM Agent Online</b>\nFull VM control active.\nExecutors: " + ", ".join(EXECUTORS.keys()))

    last_heartbeat = 0

    while True:
        try:
            now = time.time()

            # Heartbeat
            if now - last_heartbeat >= HEARTBEAT_EVERY:
                send_heartbeat()
                last_heartbeat = now

            # Poll task queue
            tasks = sb_get(
                "task_queue",
                "select=*&status=eq.pending&type=eq.vm_agent&order=priority.asc&limit=5"
            )

            if not tasks:
                time.sleep(POLL_INTERVAL)
                continue

            log.info(f"[POLL] {len(tasks)} task(s) found")

            for task in tasks:
                tid = task["id"]
                payload_raw = task.get("payload", "{}")
                try:
                    payload_dict = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                except Exception:
                    payload_dict = {}

                action = payload_dict.get("action") or task.get("task", "unknown")
                log.info(f"[TASK {tid}] action={action}")

                # Decision gate
                risk = decision_gate(action, payload_dict)
                if risk == "ask":
                    log.warning(f"[GATE] Task {tid} IRREVERSIBLE — waiting for approval")
                    sb_patch("task_queue", f"id=eq.{tid}", {"status": "waiting_approval"})
                    tg_send(
                        f"⚠️ <b>VM Agent: Approval needed</b>\n"
                        f"Task #{tid}\nAction: {action}\n"
                        f"Payload: {str(payload_dict)[:200]}\n\n"
                        f"Reply with task ID to approve."
                    )
                    log_work(str(tid), action, "blocked: needs approval", ok=False)
                    continue

                # Execute
                sb_patch("task_queue", f"id=eq.{tid}", {"status": "processing"})
                executor = EXECUTORS.get(action)
                if not executor:
                    log.warning(f"[TASK {tid}] No executor for '{action}'")
                    result = {"ok": False, "error": f"No executor for action: {action}. Available: {list(EXECUTORS.keys())}"}
                else:
                    result = executor(task)

                ok     = result.get("ok", False)
                output = result.get("output", result.get("error", str(result)))[:500]

                # Report result
                sb_patch("task_queue", f"id=eq.{tid}", {
                    "status": "completed" if ok else "failed",
                    "error": None if ok else output[:200],
                })
                log_work(str(tid), action, output, ok=ok)
                log.info(f"[TASK {tid}] ok={ok} result={output[:80]}")

                if risk == "notify":
                    status = "✅" if ok else "❌"
                    tg_send(
                        f"{status} <b>VM Agent executed: {action}</b>\n"
                        f"Task #{tid}\n{output[:300]}"
                    )

        except Exception as e:
            log.error(f"[LOOP] Error: {e}")
            time.sleep(POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)


# ── Single cycle (for cron/manual) ───────────────────────────────────────────
def run_cycle():
    """Single execution cycle — for manual runs."""
    init_db()
    log.info(f"[CYCLE] {datetime.utcnow().isoformat()}")
    send_heartbeat()

    tasks = sb_get(
        "task_queue",
        "select=*&status=eq.pending&type=eq.vm_agent&order=priority.asc&limit=5"
    )
    if not tasks:
        log.info("[POLL] No pending vm_agent tasks.")
        return

    for task in tasks:
        tid = task["id"]
        payload_raw = task.get("payload", "{}")
        try:
            payload_dict = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except Exception:
            payload_dict = {}

        action = payload_dict.get("action") or task.get("task", "unknown")
        risk   = decision_gate(action, payload_dict)

        if risk == "ask":
            sb_patch("task_queue", f"id=eq.{tid}", {"status": "waiting_approval"})
            continue

        sb_patch("task_queue", f"id=eq.{tid}", {"status": "processing"})
        executor = EXECUTORS.get(action)
        result   = executor(task) if executor else {"ok": False, "error": f"No executor: {action}"}
        ok       = result.get("ok", False)
        output   = result.get("output", result.get("error", ""))[:300]

        sb_patch("task_queue", f"id=eq.{tid}", {
            "status": "completed" if ok else "failed",
            "error": None if ok else output[:200],
        })
        log_work(str(tid), action, output, ok=ok)
        log.info(f"[TASK {tid}] ok={ok}")


# ── Systemd installer ─────────────────────────────────────────────────────────
def install_service():
    service = f"""[Unit]
Description=CORE VM Agent
After=network.target core-agi.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory={AGENT_DIR}
EnvironmentFile={AGENT_DIR}/.env
ExecStart={PYTHON_PATH} {AGENT_DIR}/core_agent.py --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    path = Path("/etc/systemd/system/core-vm-agent.service")
    path.write_text(service)
    subprocess.run(["systemctl", "daemon-reload"])
    subprocess.run(["systemctl", "enable", "core-vm-agent"])
    subprocess.run(["systemctl", "start",  "core-vm-agent"])
    print("✅ core-vm-agent service installed and started")
    print(f"   Logs: journalctl -u core-vm-agent -f")
    print(f"   Status: systemctl status core-vm-agent")


def uninstall_service():
    subprocess.run(["systemctl", "stop",    "core-vm-agent"])
    subprocess.run(["systemctl", "disable", "core-vm-agent"])
    Path("/etc/systemd/system/core-vm-agent.service").unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"])
    print("✅ core-vm-agent service removed")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CORE VM Agent")
    parser.add_argument("--install",   action="store_true", help="Install as systemd service")
    parser.add_argument("--uninstall", action="store_true", help="Remove systemd service")
    parser.add_argument("--loop",      action="store_true", help="Run continuous polling loop")
    args = parser.parse_args()

    if args.install:
        install_service()
    elif args.uninstall:
        uninstall_service()
    elif args.loop:
        run_loop()
    else:
        run_cycle()
