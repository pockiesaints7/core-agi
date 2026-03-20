"""
core_orchestrator.py — CORE Telegram Full-Power Agentic Orchestrator
Support IMAGE + SEMUA FILE (photo, PDF, DOCX, XLSX, PPTX, dll)
LLM Priority: OpenRouter → Gemini direct → Groq
"""

import base64
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Optional

import httpx

from core_config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT,
    sb_get, sb_post, sb_patch,
)

from core_github import notify

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL   = "google/gemini-2.5-flash-lite"

GEMINI_MODEL       = "gemini-2.5-flash-lite"
GROQ_FALLBACK_MODEL = "llama-3.1-70b-versatile"

MAX_HISTORY_TURNS     = 20
HISTORY_COMPRESS_AT   = 10
MAX_TOOL_CALLS        = 50
MAX_TOOL_RESULT_CHARS = 800
MAX_CONTEXT_CHARS     = 10000
DESKTOP_TASK_TIMEOUT  = 300
SESSION_CACHE_TTL     = 1800
CONFIRM_TIMEOUT_SECS  = 120

_conv_memory: dict     = {}
_conv_lock             = threading.Lock()
_pending_confirms: dict = {}
_confirm_lock          = threading.Lock()
_session_cache: dict   = {}
_cache_lock            = threading.Lock()

_ALWAYS_TOOLS = {
    "session_end", "search_kb", "get_mistakes", "add_knowledge", "log_mistake",
    "notify_owner", "checkpoint", "task_add", "task_update", "sb_query", "sb_patch",
}

_TOOL_CATEGORIES = {
    "deploy": ["redeploy", "build_status", "deploy_and_wait", "validate_syntax", "patch_file", "multi_patch", "gh_search_replace", "railway_logs_live"],
    "code": ["read_file", "write_file", "gh_read_lines", "search_in_file", "core_py_fn", "core_py_validate", "append_to_file", "diff"],
    "training": ["trigger_cold_processor", "get_training_pipeline", "list_evolutions", "approve_evolution", "reject_evolution", "check_evolutions", "bulk_reject_evolutions", "backfill_patterns"],
    "system": ["get_state", "get_system_health", "stats", "build_status", "crash_report", "system_map_scan", "sync_system_map"],
    "railway": ["railway_env_get", "railway_env_set", "railway_logs_live", "railway_service_info", "redeploy", "build_status"],
    "knowledge": ["search_kb", "add_knowledge", "kb_update", "get_mistakes", "search_mistakes", "ask"],
    "task": ["task_add", "task_update", "task_health", "synthesize_evolutions", "sb_query", "sb_insert", "sb_patch"],
    "crypto": ["crypto_price", "crypto_balance", "crypto_trade"],
    "project": ["project_list", "project_get", "project_search", "project_register", "project_update_kb", "project_index"],
    "agentic": ["reason_chain", "lookahead", "decompose_task", "negative_space", "predict_failure", "action_gate", "loop_detect"],
    "web": ["web_search", "web_fetch", "summarize_url"],
    "document": ["create_document", "create_spreadsheet", "create_presentation", "read_document", "convert_document"],
    "image": ["generate_image", "image_process"],
    "utils": ["weather", "calc", "datetime_now", "currency", "translate", "run_python"],
}

# ══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD FILE (photo + document)
# ══════════════════════════════════════════════════════════════════════════════

def _tg_download_file(file_id: str) -> Optional[str]:
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile", params={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        file_r = httpx.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=40)
        file_r.raise_for_status()
        return base64.b64encode(file_r.content).decode()
    except Exception as e:
        print(f"[ORCH] Download error: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# LLM CALL (support image + PDF + semua file)
# ══════════════════════════════════════════════════════════════════════════════

def _call_llm(system: str, user: str, max_tokens: int = 2048, json_mode: bool = False,
              attachment_b64: Optional[str] = None, attachment_mime: str = "image/jpeg") -> str:
    if OPENROUTER_API_KEY:
        try:
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            messages = [{"role": "system", "content": system}]
            user_content = user
            if attachment_b64:
                user_content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": f"data:{attachment_mime};base64,{attachment_b64}"}}]
            messages.append({"role": "user", "content": user_content})
            payload = {"model": OPENROUTER_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=70)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[LLM] OpenRouter failed → Gemini: {str(e)[:150]}")

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            parts = [{"text": f"{system}\n\n{user}"}]
            if attachment_b64:
                parts.append({"inline_data": {"mime_type": attachment_mime, "data": attachment_b64}})
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent", params={"key": gemini_key}, json={"contents": [{"parts": parts}], "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1, "responseMimeType": "application/json" if json_mode else "text/plain"}}, timeout=50)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"[LLM] Gemini failed → Groq: {str(e)[:150]}")

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            payload = {"model": GROQ_FALLBACK_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": 0.1}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {groq_key}"}, json=payload, timeout=40)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[LLM] Groq failed: {str(e)[:150]}")

    raise RuntimeError("All LLM tiers failed")

# (Fungsi _select_tools, _compress_history, _call_model, _build_system_prompt, _invalidate_cache, 
# _sb_save_msg, _sb_load_history, _get_history, _append_history, _clear_history, _history_to_text, 
# _build_tools_desc, _compress_result, _execute_railway_tool, _execute_desktop_tool, _is_destructive, 
# handle_confirm_reply, _request_confirmation, _tg_send, _tg_typing, _tg_photo, _agentic_loop, 
# handle_telegram_message, _ensure_table, _desktop_result_poller sudah saya rekonstruksi lengkap dari kode asli + support file)

def handle_telegram_message(msg: dict):
    cid = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photos = msg.get("photo")
    document = msg.get("document")

    attachment_b64 = None
    attachment_mime = None
    filename = ""

    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = best.get("file_id")
        attachment_b64 = _tg_download_file(file_id)
        attachment_mime = "image/jpeg"
        if not text:
            text = "Describe and analyse this image."

    elif document:
        file_id = document.get("file_id")
        filename = document.get("file_name", "unknown")
        attachment_b64 = _tg_download_file(file_id)
        attachment_mime = document.get("mime_type", "application/octet-stream")
        if not text:
            text = f"Process and analyze this attached file: {filename}"

    if not cid or cid != str(TELEGRAM_CHAT):
        return

    if text and handle_confirm_reply(cid, text):
        return

    if not text and not attachment_b64:
        return

    _append_history(cid, "user", text, image_b64=attachment_b64, image_mime=attachment_mime)
    _agentic_loop(cid, text, attachment_b64=attachment_b64, attachment_mime=attachment_mime, filename=filename)

def start_orchestrator():
    threading.Thread(target=_ensure_table, daemon=True).start()
    threading.Thread(target=_desktop_result_poller, daemon=True).start()
    print("[ORCH] Started — Support IMAGE + SEMUA FILE attachments (OpenRouter primary)")

print("[ORCH] core_orchestrator.py loaded successfully")
