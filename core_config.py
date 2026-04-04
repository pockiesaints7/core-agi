"""core_config.py Ã¢â‚¬â€ CORE AGI shared configuration
All env vars, constants, RateLimiter, and Supabase helpers.
Imported by all other core_* modules. Has NO imports from other core_* modules.

Part of Task 2 architecture split. core.py remains the live entry point until
smoke test passes on all modules.
"""
import hashlib
import json
import os
import time
import threading
from collections import defaultdict
from pathlib import Path

import httpx
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
_REPO_ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(_REPO_ENV)

# -- Env vars ------------------------------------------------------------------
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST      = os.environ.get("GROQ_MODEL_FAST", "llama-3.1-8b-instant")
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_SVC   = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON  = os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT     = os.environ["GITHUB_PAT"]
GITHUB_REPO    = os.environ.get("GITHUB_USERNAME", "pockiesaints7") + "/core-agi"
MCP_SECRET     = os.environ["MCP_SECRET"]
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", MCP_SECRET)
SUPABASE_PAT   = os.environ.get("SUPABASE_PAT", "")  # Management API PAT for DB introspection
SUPABASE_REF   = os.environ.get("SUPABASE_REF", "bwywfbiprbdkhlbwyprw")  # Project ref
PORT           = int(os.environ.get("PORT", 8081))
# -- Env parsing helpers ------------------------------------------------------

def _env_clean(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return default
    value = value.strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


def _env_int(name: str, default: str | int) -> int:
    try:
        return int(_env_clean(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: str | float) -> float:
    try:
        return float(_env_clean(name, str(default)))
    except Exception:
        return float(default)


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _validate_telegram_token_layout() -> None:
    telegram_alias = _env_clean("TELEGRAM_TOKEN")
    if telegram_alias and telegram_alias != TELEGRAM_TOKEN:
        raise RuntimeError("core-agi: TELEGRAM_TOKEN conflicts with TELEGRAM_BOT_TOKEN")
    if _env_clean("BOT_TOKEN"):
        raise RuntimeError("core-agi: unexpected BOT_TOKEN present; use TELEGRAM_BOT_TOKEN only")
    print(
        f"[CONFIG] core-agi telegram token fingerprint={_token_fingerprint(TELEGRAM_TOKEN)} "
        f"source=TELEGRAM_BOT_TOKEN"
    )


_validate_telegram_token_layout()
SESSION_TTL_H  = 8

MCP_PROTOCOL_VERSION = "2024-11-05"

# Training config
COLD_HOT_THRESHOLD        = 5   # lowered from 10 -- faster signal processing
COLD_TIME_THRESHOLD       = 21600  # 6h -- lowered from 24h for faster cold runs
COLD_KB_GROWTH_THRESHOLD  = 100
PATTERN_EVO_THRESHOLD     = 3
KNOWLEDGE_AUTO_CONFIDENCE = 0.7

# KB mining config
KB_MINE_BATCH_SIZE       = 20
KB_MINE_RATIO_THRESHOLD  = 20

# Self-sync config
CORE_SELF_STALE_DAYS = 7

# -- Rate limiter --------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self.calls = defaultdict(list)
        try:
            self.c = json.load(open("resource_ceilings.json"))
        except:
            self.c = {
                "groq_calls_per_hour": 200,
                "supabase_writes_per_hour": 500,
                "github_pushes_per_hour": 20,
                "telegram_messages_per_hour": 30,
                "mcp_tool_calls_per_minute": 30,
            }

    def _ok(self, key, window, limit):
        now = time.time()
        self.calls[key] = [t for t in self.calls[key] if now - t < window]
        if len(self.calls[key]) >= limit:
            return False
        self.calls[key].append(now)
        return True

    def tg(self):       return True  # no limit -- loop timer is the throttle
    def gh(self):       return self._ok("gh",  3600, self.c.get("github_pushes_per_hour", 20))
    def sbw(self):      return True  # no limit -- loop timer is the throttle
    def mcp(self, sid): return self._ok(f"mcp:{sid}", 60, self.c.get("mcp_tool_calls_per_minute", 30))

L = RateLimiter()

# -- Supabase helpers ----------------------------------------------------------
_SUPABASE_CIRCUIT_UNTIL = 0.0
_SUPABASE_CIRCUIT_ERRORS = 0
_SUPABASE_CIRCUIT_COOLDOWN_SECS = int(os.environ.get("SUPABASE_CIRCUIT_COOLDOWN_SECS", "180"))
_SUPABASE_CIRCUIT_THRESHOLD = int(os.environ.get("SUPABASE_CIRCUIT_THRESHOLD", "3"))
_SB_SCHEMA_CACHE_UNTIL = 0.0
_SB_SCHEMA_CACHE_LOCK = threading.Lock()


def _sbh(svc=False):
    k = SUPABASE_SVC if svc else SUPABASE_ANON
    return {"apikey": k, "Authorization": f"Bearer {k}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

def _sbh_count_svc():
    return {"apikey": SUPABASE_SVC, "Authorization": f"Bearer {SUPABASE_SVC}",
            "Prefer": "count=exact"}


def _sb_circuit_open() -> bool:
    return time.time() < _SUPABASE_CIRCUIT_UNTIL


def _sb_circuit_note(response=None):
    global _SUPABASE_CIRCUIT_ERRORS, _SUPABASE_CIRCUIT_UNTIL
    _SUPABASE_CIRCUIT_ERRORS += 1
    status = getattr(response, "status_code", None)
    text = ""
    if response is not None:
        try:
            text = (response.text or "")[:200]
        except Exception:
            text = ""
    if status in {429, 500, 502, 503, 504} or "schema cache" in text.lower() or "timed out" in text.lower() or response is None:
        if _SUPABASE_CIRCUIT_ERRORS >= _SUPABASE_CIRCUIT_THRESHOLD:
            _SUPABASE_CIRCUIT_UNTIL = time.time() + _SUPABASE_CIRCUIT_COOLDOWN_SECS
            print(f"[SB] circuit open for {_SUPABASE_CIRCUIT_COOLDOWN_SECS}s after repeated failures")


def _sb_circuit_reset():
    global _SUPABASE_CIRCUIT_ERRORS, _SUPABASE_CIRCUIT_UNTIL
    _SUPABASE_CIRCUIT_ERRORS = 0
    _SUPABASE_CIRCUIT_UNTIL = 0.0


_SB_SCHEMA_BOOTSTRAP_ATTEMPTED = False


def _sb_schema_missing_response(response) -> bool:
    status = getattr(response, "status_code", None)
    if status not in {400, 404, 409, 422}:
        return False
    try:
        text = (response.text or "").lower()
    except Exception:
        text = ""
    if not text:
        return False
    needles = (
        "does not exist",
        "relation",
        "column",
        "pgrst",
        "42p01",
        "42703",
        "unknown table",
        "unknown column",
    )
    return any(n in text for n in needles)


def _sb_bootstrap_schema_once(reason: str) -> None:
    global _SB_SCHEMA_BOOTSTRAP_ATTEMPTED
    if _SB_SCHEMA_BOOTSTRAP_ATTEMPTED:
        return
    _SB_SCHEMA_BOOTSTRAP_ATTEMPTED = True
    try:
        from core_supabase_bootstrap import bootstrap_supabase as _bootstrap_supabase
        result = _bootstrap_supabase()
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        print(f"[SB] bootstrap ({reason}) -> {'ok' if ok else 'failed'}")
    except Exception as exc:
        print(f"[SB] bootstrap ({reason}) error: {exc}")


def _sb_schema_cache_response(response) -> bool:
    status = getattr(response, "status_code", None)
    if status != 503:
        return False
    try:
        text = (response.text or "").lower()
    except Exception:
        text = ""
    return "schema cache" in text or "pgrst002" in text


def _sb_retry_delay(attempt: int) -> float:
    # Longer backoff is only for PostgREST schema-cache warmup; it is not used for regular failures.
    return min(0.5 * (2 ** attempt), 4.0)


def _sb_schema_cache_cooldown_open() -> bool:
    return time.time() < _SB_SCHEMA_CACHE_UNTIL


def _sb_schema_cache_cooldown(delay: float, table: str) -> None:
    global _SB_SCHEMA_CACHE_UNTIL
    _SB_SCHEMA_CACHE_UNTIL = max(_SB_SCHEMA_CACHE_UNTIL, time.time() + delay)
    print(f"[SB GET] {table} schema cache warming; retrying in {delay:.2f}s")


def sb_get(t, qs="", svc=False):
    if _sb_circuit_open() or _sb_schema_cache_cooldown_open():
        return []
    with _SB_SCHEMA_CACHE_LOCK:
        if _sb_circuit_open() or _sb_schema_cache_cooldown_open():
            return []
        try:
            last_response = None
            for attempt in range(5):
                r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}", headers=_sbh(svc), timeout=15)
                last_response = r
                if r.is_success:
                    _sb_circuit_reset()
                    return r.json()
                if _sb_schema_missing_response(r):
                    print(f"[SB GET] {t} missing schema: {r.status_code} {r.text[:200]}")
                    _sb_bootstrap_schema_once(f"get:{t}")
                    try:
                        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}", headers=_sbh(svc), timeout=15)
                        last_response = r
                        if r.is_success:
                            _sb_circuit_reset()
                            return r.json()
                    except Exception as retry_exc:
                        print(f"[SB GET] {t} retry error after bootstrap: {retry_exc}")
                if _sb_schema_cache_response(r):
                    if attempt < 4:
                        delay = _sb_retry_delay(attempt)
                        _sb_schema_cache_cooldown(delay, t)
                        time.sleep(delay)
                        continue
                    _sb_schema_cache_cooldown(_sb_retry_delay(attempt), t)
                    return []
                break
            print(f"[SB GET] {t} failed: {last_response.status_code} {last_response.text[:200]}")
            _sb_circuit_note(last_response)
            return []
        except Exception as e:
            print(f"[SB GET] {t} error: {e}")
            _sb_circuit_note()
            return []

def sb_post(t, d):
    if not L.sbw() or _sb_circuit_open(): return False
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
        if r.is_success:
            _sb_circuit_reset()
            return True
        if _sb_schema_missing_response(r):
            print(f"[SB POST] {t} missing schema: {r.status_code} {r.text[:200]}")
            _sb_bootstrap_schema_once(f"post:{t}")
            try:
                r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
                if r.is_success:
                    _sb_circuit_reset()
                    return True
            except Exception as retry_exc:
                print(f"[SB POST] {t} retry error after bootstrap: {retry_exc}")
        print(f"[SB POST] {t} failed: {r.status_code} {r.text[:200]}")
        _sb_circuit_note(r)
        return False
    except Exception as e:
        print(f"[SB POST] {t} error: {e}")
        _sb_circuit_note()
        return False


def sb_post_critical(t, d):
    if _sb_circuit_open(): return False
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
        if r.is_success:
            _sb_circuit_reset()
            return True
        if _sb_schema_missing_response(r):
            print(f"[SB CRITICAL] {t} missing schema: {r.status_code} {r.text[:200]}")
            _sb_bootstrap_schema_once(f"critical:{t}")
            try:
                r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
                if r.is_success:
                    _sb_circuit_reset()
                    return True
            except Exception as retry_exc:
                print(f"[SB CRITICAL] {t} retry error after bootstrap: {retry_exc}")
        print(f"[SB CRITICAL] {t} failed: {r.status_code} {r.text[:200]}")
        _sb_circuit_note(r)
        return False
    except Exception as e:
        print(f"[SB CRITICAL] {t} error: {e}")
        _sb_circuit_note()
        return False


def sb_patch(t, m, d):
    if not L.sbw() or _sb_circuit_open(): return False
    try:
        r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), json=d, timeout=15)
        if r.is_success:
            _sb_circuit_reset()
            return True
        if _sb_schema_missing_response(r):
            print(f"[SB PATCH] {t} missing schema: {r.status_code} {r.text[:200]}")
            _sb_bootstrap_schema_once(f"patch:{t}")
            try:
                r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), json=d, timeout=15)
                if r.is_success:
                    _sb_circuit_reset()
                    return True
            except Exception as retry_exc:
                print(f"[SB PATCH] {t} retry error after bootstrap: {retry_exc}")
        print(f"[SB PATCH] {t} failed: {r.status_code} {r.text[:200]}")
        _sb_circuit_note(r)
        return False
    except Exception as e:
        print(f"[SB PATCH] {t} error: {e}")
        _sb_circuit_note()
        return False


def sb_upsert(t, d, on_conflict):
    if not L.sbw() or _sb_circuit_open(): return False
    try:
        h = {**_sbh(True), "Prefer": "resolution=merge-duplicates,return=minimal"}
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}?on_conflict={on_conflict}", headers=h, json=d, timeout=15)
        if r.is_success:
            _sb_circuit_reset()
            return True
        if _sb_schema_missing_response(r):
            print(f"[SB UPSERT] {t} missing schema: {r.status_code} {r.text[:200]}")
            _sb_bootstrap_schema_once(f"upsert:{t}")
            try:
                r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}?on_conflict={on_conflict}", headers=h, json=d, timeout=15)
                if r.is_success:
                    _sb_circuit_reset()
                    return True
            except Exception as retry_exc:
                print(f"[SB UPSERT] {t} retry error after bootstrap: {retry_exc}")
        print(f"[SB UPSERT] {t} failed: {r.status_code} {r.text[:200]}")
        _sb_circuit_note(r)
        return False
    except Exception as e:
        print(f"[SB UPSERT] {t} error: {e}")
        _sb_circuit_note()
        return False


def sb_delete(t, m):
    """DELETE rows matching filter string m from table t.
    m must be a non-empty PostgREST filter string e.g. 'id=eq.123'.
    Returns False immediately if m is empty -- never allows full-table delete."""
    if not m or not str(m).strip():
        print(f"[SB DELETE] BLOCKED: empty filter on table {t} -- full-table delete not allowed")
        return False
    if not L.sbw() or _sb_circuit_open(): return False
    try:
        r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), timeout=15)
        if r.is_success:
            _sb_circuit_reset()
            return True
        if _sb_schema_missing_response(r):
            print(f"[SB DELETE] {t} missing schema: {r.status_code} {r.text[:200]}")
            _sb_bootstrap_schema_once(f"delete:{t}")
            try:
                r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), timeout=15)
                if r.is_success:
                    _sb_circuit_reset()
                    return True
            except Exception as retry_exc:
                print(f"[SB DELETE] {t} retry error after bootstrap: {retry_exc}")
        print(f"[SB DELETE] {t} failed: {r.status_code} {r.text[:200]}")
        _sb_circuit_note(r)
        return False
    except Exception as e:
        print(f"[SB DELETE] {t} error: {e}")
        _sb_circuit_note()
        return False

# -- Telegram notify helper ----------------------------------------------------
def notify(text: str, chat_id: str = "") -> bool:
    """Send a Telegram message. Falls back to TELEGRAM_CHAT if chat_id not given.
    Non-blocking on failure Ã¢â‚¬â€ always returns bool."""
    cid = chat_id or TELEGRAM_CHAT
    if not TELEGRAM_TOKEN or not cid:
        print(f"[NOTIFY] Cannot send Ã¢â‚¬â€ TOKEN or chat_id missing")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text[:4096], "parse_mode": "HTML"},
            timeout=10,
        )
        return r.is_success
    except Exception as e:
        print(f"[NOTIFY] Failed: {e}")
        return False


# -- Groq chat helper ----------------------------------------------------------
def groq_chat(system: str, user: str, model: str = None, max_tokens: int = 1024) -> str:
    """Shared Groq chat helper.

    Behavior:
    - prefers the requested model, then falls back to GROQ_FAST on transient failure
    - retries transient HTTP/network failures a few times with short backoff
    - keeps the hard 20s timeout so the pipeline never hangs
    """
    import time as _time

    primary = model or GROQ_MODEL
    fallback = GROQ_FAST if GROQ_FAST and GROQ_FAST != primary else ""
    candidates = [primary] + ([fallback] if fallback else [])
    transient_statuses = {429, 500, 502, 503, 504}
    last_err = None

    for idx, candidate in enumerate(candidates):
        for attempt in range(3):
            try:
                t0 = _time.monotonic()
                r = httpx.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": candidate,
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                    timeout=20,  # Hard 20s Ã¢â‚¬â€ Groq is fast, >20s means something is wrong
                )
                elapsed = round(_time.monotonic() - t0, 2)
                if elapsed > 5:
                    print(f"[GROQ] SLOW response: {elapsed}s model={candidate}")
                if r.status_code in transient_statuses:
                    last_err = f"{r.status_code}: {r.text[:240]}"
                    if attempt < 2:
                        _time.sleep(min(2 ** attempt, 4))
                        continue
                r.raise_for_status()
                payload = r.json()
                choices = payload.get("choices") or []
                if not choices:
                    raise RuntimeError(f"Groq returned no choices (model={candidate})")
                message = choices[0].get("message") or {}
                content = (message.get("content") or "").strip()
                if not content:
                    raise RuntimeError(f"Groq returned empty content (model={candidate})")
                return content
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as e:
                last_err = str(e)
                if attempt < 2:
                    _time.sleep(min(2 ** attempt, 4))
                    continue
            except Exception as e:
                last_err = str(e)
                # Non-transient error: try fallback model once, otherwise fail out.
                break
        print(f"[GROQ] Candidate failed: model={candidate} err={last_err}")

    raise RuntimeError(f"Groq chat failed after {len(candidates)} candidate(s): {last_err}")


# -- Gemini chat helper with round-robin key rotation -------------------------
def _load_gemini_keys() -> list[str]:
    """Load GEMINI_KEYS from .env as a stable ordered list."""
    raw = os.getenv("GEMINI_KEYS", "")
    seen = set()
    keys: list[str] = []
    for part in raw.split(","):
        key = part.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


_GEMINI_KEYS = _load_gemini_keys()
_GEMINI_KEY_INDEX = 0
_GEMINI_MODEL = "gemini-2.5-flash"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

def gemini_chat(system: str, user: str, max_tokens: int = 2048, json_mode: bool = False, model: str = "") -> str:
    """LLM chat with full fallback chain:
    1. OpenRouter (model param or OPENROUTER_MODEL env Ã¢â‚¬â€ supports Gemini 2.5 Flash, Opus, any model)
    2. Gemini direct API (round-robin across all GEMINI_KEYS Ã¢â‚¬â€ up to 11 keys)
    3. Groq (strongest free model Ã¢â‚¬â€ final safety net)
    model param: pass any OpenRouter model string to override (e.g. "anthropic/claude-opus-4-5")
    json_mode=True: instructs model to return valid JSON only.
    """
    active_model = model or OPENROUTER_MODEL

    # Ã¢â€â‚¬Ã¢â€â‚¬ Tier 1: OpenRouter Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    if OPENROUTER_API_KEY:
        # Proper system/user separation for best reasoning quality
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload = {
            "model": active_model,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_err = None
        for attempt in range(3):
            try:
                _t0 = time.monotonic()
                r = httpx.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": f"https://{os.environ.get('PUBLIC_DOMAIN', 'core-agi.duckdns.org')}",
                        "X-Title": "CORE AGI",
                    },
                    json=payload,
                    timeout=60,  # 60s Ã¢â‚¬â€ supports long Opus/agentic calls
                )
                _elapsed = round(time.monotonic() - _t0, 2)
                if _elapsed > 5:
                    print(f"[OPENROUTER] SLOW: {_elapsed}s model={active_model} attempt={attempt+1}")
                if r.status_code == 429:
                    last_err = "429 rate limit"
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                last_err = str(e)
                continue
        # Don't raise Ã¢â‚¬â€ fall through to Gemini direct
        print(f"[OPENROUTER] Failed after 3 attempts: {last_err} Ã¢â‚¬â€ trying Gemini direct")

    # Ã¢â€â‚¬Ã¢â€â‚¬ Tier 2: Gemini direct (round-robin all keys) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    global _GEMINI_KEY_INDEX
    if _GEMINI_KEYS:
        attempts = len(_GEMINI_KEYS)
        last_err = None
        for _ in range(attempts):
            key = _GEMINI_KEYS[_GEMINI_KEY_INDEX % len(_GEMINI_KEYS)]
            _GEMINI_KEY_INDEX = (_GEMINI_KEY_INDEX + 1) % len(_GEMINI_KEYS)
            try:
                prompt = f"{system}\n\n{user}" if system else user
                r = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent",
                    params={"key": key},
                    headers={"Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {
                              "maxOutputTokens": max_tokens,
                              "temperature": 0.1,
                              **({("responseMimeType"): "application/json"} if json_mode else {})
                          },
                          "safetySettings": [
                              {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                              {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                              {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                              {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                          ]},
                    timeout=30,
                )
                if r.status_code == 429:
                    last_err = f"429 on key {(_GEMINI_KEY_INDEX - 1) % len(_GEMINI_KEYS)}"
                    time.sleep(2)
                    continue
                r.raise_for_status()
                resp_json = r.json()
                candidate = resp_json.get("candidates", [{}])[0]
                parts = candidate.get("content", {}).get("parts", [])
                if not parts:
                    last_err = f"empty parts (finish={candidate.get('finishReason','?')})"
                    continue
                return parts[0]["text"].strip()
            except Exception as e:
                last_err = str(e)
                continue
        # Don't raise Ã¢â‚¬â€ fall through to Groq
        print(f"[GEMINI] All {attempts} keys exhausted: {last_err} Ã¢â‚¬â€ trying Groq")

    # Ã¢â€â‚¬Ã¢â€â‚¬ Tier 3: Groq (strongest free model Ã¢â‚¬â€ final safety net) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    try:
        return groq_chat(system=system, user=user, model=GROQ_MODEL, max_tokens=max_tokens)
    except Exception as e:
        raise RuntimeError(
            f"All LLM tiers failed. "
            f"OpenRouter: {'skipped (no key)' if not OPENROUTER_API_KEY else 'failed'}. "
            f"Gemini: {len(_GEMINI_KEYS)} keys tried. "
            f"Groq: {e}"
        )


def build_live_schema(supabase_ref: str = "", supabase_pat: str = "") -> dict:
    """
    Build schema registry from actual Supabase tables at startup.
    Merges live column definitions into _SB_SCHEMA at import time.
    Falls back gracefully (returns {}) if Management API unavailable or PAT missing.

    Args:
        supabase_ref: Supabase project ref. Defaults to module-level SUPABASE_REF.
        supabase_pat: Supabase Management API PAT. Defaults to module-level SUPABASE_PAT.
    """
    try:
        # Use passed args first, fall back to module-level constants
        ref = supabase_ref or SUPABASE_REF
        pat = supabase_pat or SUPABASE_PAT
        if not pat:
            print("[SCHEMA] build_live_schema: SUPABASE_PAT not set Ã¢â‚¬â€ skipping live fetch")
            return {}
        if not ref:
            print("[SCHEMA] build_live_schema: SUPABASE_REF not set Ã¢â‚¬â€ skipping live fetch")
            return {}

        resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
            },
            json={"query": """
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
            """},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(f"[SCHEMA] Live schema fetch failed: {resp.status_code} {resp.text[:200]}")
            return {}

        rows = resp.json()
        if not isinstance(rows, list):
            print(f"[SCHEMA] Unexpected response format: {type(rows)}")
            return {}

        live_schema: dict = {}
        for row in rows:
            table = row.get("table_name", "")
            col   = row.get("column_name", "")
            dtype = row.get("data_type", "text")
            if not table or not col:
                continue
            if table not in live_schema:
                live_schema[table] = {"columns": {}}
            live_schema[table]["columns"][col] = dtype

        print(f"[SCHEMA] Live schema loaded: {len(live_schema)} tables")
        return live_schema
    except Exception as e:
        print(f"[SCHEMA] Live schema failed (using hardcoded fallback): {e}")
        return {}


# Ã¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢Â
# TOOL CATEGORY KEYWORDS Ã¢â‚¬â€ single source of truth
# Used by: core_orchestrator._build_live_categories()
#          core_web.t_list_tools()
# Update here ONLY when adding a new tool category domain.
# Keys are category names. Values are keyword fragments matched against tool names.
# A tool is assigned to the first category whose keywords appear in the tool name.
# Tools that match no keywords go to "misc" automatically.
# Ã¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢Â
TOOL_CATEGORY_KEYWORDS: dict = {
    "deploy":    ["redeploy", "deploy", "build_status", "validate_syntax",
                  "patch_file", "multi_patch", "gh_search_replace", "railway_logs",
                  "replace_fn", "smart_patch", "register_tool", "rollback"],
    "code":      ["read_file", "write_file", "gh_read", "search_in_file",
                  "core_py", "append_to_file", "diff", "repo_component", "repo_graph"],
    "training":  ["cold_processor", "training_pipeline", "evolution", "reflection",
                  "backfill", "synthesize"],
    "system":    ["get_state", "health", "stats", "crash", "system_map",
                  "sync_system", "session_start", "session_end", "repo_map"],
    "railway":   ["railway_env", "railway_service", "railway_logs"],
    "knowledge": ["search_kb", "add_knowledge", "kb_update", "get_mistakes",
                  "search_mistakes", "ask", "public_evidence", "ingest_knowledge"],
    "task":      ["task_add", "task_update", "task_health", "sb_query",
                  "sb_insert", "sb_patch", "sb_upsert", "sb_delete"],
    "crypto":    ["crypto", "binance"],
    "project":   ["project_"],
    "agentic":   ["reason_chain", "lookahead", "decompose", "negative_space",
                  "predict_failure", "action_gate", "loop_detect", "goal_check",
                  "circuit_breaker", "mid_task", "assert_source"],
    "web":       ["web_search", "web_fetch", "summarize_url"],
    "document":  ["create_document", "create_spreadsheet", "create_presentation",
                  "read_document", "convert_document", "read_pdf", "read_image"],
    "image":     ["generate_image", "image_process"],
    "utils":     ["weather", "calc", "datetime", "currency", "translate",
                  "run_python", "list_tools", "get_tool_info", "get_table_schema",
                  "notify", "tool_stats", "tool_health", "debug_fn", "backlog",
                  "changelog", "backup"],
}

# Tools always included in every model call regardless of category routing.
# These are the core brain tools Ã¢â‚¬â€ they should always be available.
TOOL_ALWAYS_INCLUDE: set = {
    "search_kb", "get_mistakes", "list_tools", "get_tool_info",
    "get_behavioral_rules", "get_table_schema",
}







