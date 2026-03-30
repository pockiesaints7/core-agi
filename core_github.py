"""core_github.py — CORE AGI GitHub + Telegram helpers
Extracted from core.py lines 232-318 + notify/set_webhook.
Imported by core_train.py, core_tools.py, core_main.py.

Depends on: core_config (L, GITHUB_PAT, GITHUB_REPO, TELEGRAM_TOKEN, TELEGRAM_CHAT)
"""
import os
import base64
import json

import httpx
import html

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(path=None, override=False):
        from pathlib import Path as _Path

        def _apply(candidate: _Path) -> bool:
            if not candidate.exists():
                return False
            loaded = False
            try:
                for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    if override or key not in os.environ:
                        os.environ[key] = value
                    loaded = True
            except Exception:
                return False
            return loaded

        loaded_any = False
        if path is None:
            roots = [
                _Path.cwd() / ".env",
                _Path(__file__).resolve().parent / ".env",
                _Path(__file__).resolve().parent.parent / ".env",
            ]
        else:
            candidate = _Path(path)
            roots = [candidate if candidate.is_absolute() else _Path.cwd() / candidate, candidate]
        for candidate in roots:
            loaded_any = _apply(candidate) or loaded_any
        return loaded_any
load_dotenv()

from core_config import (
    L,
    GITHUB_PAT, GITHUB_REPO,
    TELEGRAM_TOKEN, TELEGRAM_CHAT, TELEGRAM_WEBHOOK_SECRET,
)

# -- Telegram ------------------------------------------------------------------
def _telegram_send(msg: str, cid=None, parse_mode: str | None = "HTML"):
    payload = {
        "chat_id": cid or TELEGRAM_CHAT,
        "text": msg[:4000],
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
        timeout=10,
    )


def notify(msg, cid=None):
    if not L.tg(): return False
    try:
        text = str(msg)
        r = _telegram_send(text, cid=cid, parse_mode="HTML")
        if not r.is_success:
            # Retry without parse_mode so malformed HTML or raw angle brackets
            # do not fail delivery for an otherwise valid message.
            if r.status_code == 400 and "parse entities" in r.text.lower():
                print(f"[TG] HTML parse failed; retrying plain text: {r.text[:100]}")
                r2 = _telegram_send(text, cid=cid, parse_mode=None)
                if r2.is_success:
                    print("[TG] plain-text fallback succeeded")
                    return True
                print(f"[TG] fallback failed: {r2.status_code} {r2.text[:100]}")
                return False
            print(f"[TG] failed: {r.status_code} {r.text[:100]}")
            return False
        return True
    except Exception as e:
        print(f"[TG] {e}")
        return False


def notify_owner(msg):
    """Alias used in queue_poller and Telegram bot handler."""
    return notify(msg)


def set_telegram_commands(commands, scope: str = "default"):
    """Set Telegram command menu for the owner chat.
    commands: list of {"command": "...", "description": "..."} dicts.
    scope: reserved for future per-chat command scopes.
    """
    if not TELEGRAM_TOKEN:
        print("[CORE] Telegram commands skipped: missing token")
        return False
    payload = {"commands": commands}
    if scope and scope != "default":
        payload["scope"] = scope
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
            json=payload,
            timeout=10,
        )
        print(f"[CORE] Telegram commands response: {resp.text}")
        return resp.is_success
    except Exception as e:
        print(f"[CORE] Telegram commands error: {e}")
        return False


def set_webhook():
    target_domain = os.environ.get("PUBLIC_DOMAIN", "core-agi.duckdns.org").strip()
    if "://" in target_domain:
        target_domain = target_domain.split("://", 1)[1]
    if target_domain.endswith(":80"):
        target_domain = target_domain[:-3]
    target_url = f"https://{target_domain}/webhook"

    print(f"[CORE] Setting webhook to: {target_url}")
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            data={
                "url": target_url,
                "secret_token": TELEGRAM_WEBHOOK_SECRET,
            },
            timeout=10,
        )
        print(f"[CORE] Webhook Response: {resp.text}")
    except Exception as e:
        print(f"[CORE] Webhook Error: {e}")


# -- GitHub --------------------------------------------------------------------
def _ghh():
    return {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}


def gh_read(path, repo=None):
    r = httpx.get(f"https://api.github.com/repos/{repo or GITHUB_REPO}/contents/{path}",
                  headers=_ghh(), timeout=15)
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode()


def gh_write(path, content, msg, repo=None):
    if not L.gh(): return False
    repo = repo or GITHUB_REPO
    h = _ghh()
    sha = None
    try:
        sha = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                        headers=h, timeout=10).json().get("sha")
    except: pass
    p = {"message": msg, "content": base64.b64encode(content.encode()).decode()}
    if sha: p["sha"] = sha
    return httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}",
                     headers=h, json=p, timeout=20).is_success


def _gh_blob_read(path, repo=None):
    """Read file via Git Blobs API — no size limit, works for any file size."""
    repo = repo or GITHUB_REPO
    h = _ghh()
    ref = httpx.get(f"https://api.github.com/repos/{repo}/git/ref/heads/main", headers=h, timeout=10)
    ref.raise_for_status()
    commit = httpx.get(f"https://api.github.com/repos/{repo}/git/commits/{ref.json()['object']['sha']}",
                       headers=h, timeout=10)
    commit.raise_for_status()
    tree = httpx.get(f"https://api.github.com/repos/{repo}/git/trees/{commit.json()['tree']['sha']}",
                     headers=h, timeout=10)
    tree.raise_for_status()
    blob = next((f for f in tree.json()["tree"] if f["path"] == path), None)
    if not blob: raise FileNotFoundError(f"{path} not found in repo")
    r = httpx.get(f"https://api.github.com/repos/{repo}/git/blobs/{blob['sha']}",
                  headers={**h, "Accept": "application/vnd.github.v3.raw"}, timeout=30)
    r.raise_for_status()
    return r.text


def _gh_blob_write(path, content, message, repo=None):
    """Write file via Git Trees API — atomic commit, no size limit."""
    repo = repo or GITHUB_REPO
    h = _ghh()
    ref = httpx.get(f"https://api.github.com/repos/{repo}/git/ref/heads/main", headers=h, timeout=10)
    ref.raise_for_status()
    current_sha = ref.json()["object"]["sha"]
    commit = httpx.get(f"https://api.github.com/repos/{repo}/git/commits/{current_sha}",
                       headers=h, timeout=10)
    commit.raise_for_status()
    tree_sha = commit.json()["tree"]["sha"]
    blob_r = httpx.post(f"https://api.github.com/repos/{repo}/git/blobs", headers=h,
                        json={"content": content, "encoding": "utf-8"}, timeout=60)
    blob_r.raise_for_status()
    new_blob_sha = blob_r.json()["sha"]
    tree_r = httpx.post(f"https://api.github.com/repos/{repo}/git/trees", headers=h,
                        json={"base_tree": tree_sha, "tree": [{"path": path, "mode": "100644",
                              "type": "blob", "sha": new_blob_sha}]}, timeout=20)
    tree_r.raise_for_status()
    new_tree_sha = tree_r.json()["sha"]
    commit_r = httpx.post(f"https://api.github.com/repos/{repo}/git/commits", headers=h,
                          json={"message": message, "tree": new_tree_sha, "parents": [current_sha]},
                          timeout=15)
    commit_r.raise_for_status()
    new_commit_sha = commit_r.json()["sha"]
    httpx.patch(f"https://api.github.com/repos/{repo}/git/refs/heads/main", headers=h,
                json={"sha": new_commit_sha}, timeout=15).raise_for_status()
    return new_commit_sha





