"""auto_deploy.py — GitHub webhook auto-deploy for CORE AGI
Sends Telegram notification on deploy success or failure.
"""
import hashlib
import hmac
import json
import os
import subprocess
import threading
import urllib.request
import ssl
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import uvicorn

GITHUB_SECRET  = os.environ.get("GITHUB_WEBHOOK_SECRET", "core_deploy_2026")
REPO_DIR       = "/home/ubuntu/core-agi"
SERVICE_NAME   = "core-agi"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

app = FastAPI()


def tg(msg: str):
    """Send Telegram notification."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        body = json.dumps({
            "chat_id": TELEGRAM_CHAT,
            "text": msg[:4000],
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        urllib.request.urlopen(req, context=ctx, timeout=10)
    except Exception as e:
        print(f"[DEPLOY] tg_send error: {e}")


def do_deploy(pusher: str = "", branch: str = "", commit_msg: str = ""):
    """Pull latest from GitHub and restart CORE. Notify on result."""
    ts = datetime.utcnow().strftime("%H:%M:%S UTC")
    print(f"[DEPLOY] Starting deploy — {ts}")
    tg(
        f"🔄 <b>Deploy started</b>\n"
        f"Branch: {branch or 'main'}\n"
        f"By: {pusher or 'unknown'}\n"
        f"Commit: {commit_msg[:80] or 'n/a'}\n"
        f"Time: {ts}"
    )

    # Step 1 — git pull
    pull = subprocess.run(
        ["git", "-C", REPO_DIR, "pull"],
        capture_output=True, text=True, timeout=60
    )
    if pull.returncode != 0:
        err = (pull.stdout + pull.stderr).strip()[:500]
        print(f"[DEPLOY] git pull failed: {err}")
        tg(f"❌ <b>Deploy failed — git pull error</b>\n<code>{err}</code>")
        return

    pull_out = (pull.stdout + pull.stderr).strip()[:200]
    print(f"[DEPLOY] git pull OK: {pull_out}")

    # Step 2 — restart service
    restart = subprocess.run(
        ["sudo", "systemctl", "restart", SERVICE_NAME],
        capture_output=True, text=True, timeout=30
    )
    if restart.returncode != 0:
        err = (restart.stdout + restart.stderr).strip()[:300]
        print(f"[DEPLOY] restart failed: {err}")
        tg(f"❌ <b>Deploy failed — service restart error</b>\n<code>{err}</code>")
        return

    # Step 3 — wait and verify service is running
    import time
    time.sleep(5)
    status = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True, text=True
    )
    is_active = status.stdout.strip() == "active"

    if is_active:
        print(f"[DEPLOY] Done! Service active.")
        tg(
            f"✅ <b>Deploy successful</b>\n"
            f"{pull_out}\n"
            f"Service: {SERVICE_NAME} running"
        )
    else:
        # Get last 10 lines of journal for diagnosis
        logs = subprocess.run(
            ["sudo", "journalctl", "-u", SERVICE_NAME, "-n", "10", "--no-pager"],
            capture_output=True, text=True
        )
        log_out = logs.stdout.strip()[-500:]
        print(f"[DEPLOY] Service not active after restart!")
        tg(
            f"❌ <b>Deploy failed — service not active</b>\n"
            f"<code>{log_out}</code>"
        )


@app.post("/deploy")
async def deploy(req: Request):
    body = await req.body()
    sig  = req.headers.get("X-Hub-Signature-256", "")
    mac  = "sha256=" + hmac.new(GITHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, sig):
        raise HTTPException(403, "Invalid signature")

    try:
        payload     = json.loads(body)
        pusher      = payload.get("pusher", {}).get("name", "")
        branch      = payload.get("ref", "").replace("refs/heads/", "")
        commit_msg  = payload.get("head_commit", {}).get("message", "")
    except Exception:
        pusher = branch = commit_msg = ""

    threading.Thread(
        target=do_deploy,
        args=(pusher, branch, commit_msg),
        daemon=True
    ).start()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("DEPLOY_PORT", 9000))
    print(f"[DEPLOY] Auto-deploy service starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
