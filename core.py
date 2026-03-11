"""CORE v5.0 — Recursive Self-Improvement Architecture
Owner: REINVAGNAR
Step status is dynamic — always read from SESSION.md on GitHub.
Do NOT hardcode step numbers anywhere in this file.

Fix log:
  2026-03-11e: t_state() fetches operating_context.json + SESSION.md from GitHub.
  2026-03-11f: cold_processor uses Counter for batch freq counting.
  2026-03-11g: cold_reflections insert uses sb_post_critical (bypasses rate limiter).
  2026-03-11h: All hardcoded step labels removed. Step derived dynamically from SESSION.md.
               Root cause: step labels in root(), startup(), on_start(), Telegram /start
               were hardcoded to 'Step 3' even after system advanced to Step 5.
               Fix: get_current_step() helper reads SESSION.md live on every call.
  2026-03-11i: processed_by_cold filter changed from eq.false/True to eq.0/1 (integer).
               Root cause: Supabase PostgREST rejects Python bool in querystring filter.
               Fix: use 0/1 in all querystring filters; keep Python bool in JSON body (ok there).
  2026-03-11j: Added POST /patch endpoint — surgical find-and-replace from claude.ai.
               Accepts {path, old_str, new_str, message, secret}. Reuses gh_search_replace.
               Purpose: avoid full-file rewrites from claude.ai which waste GitHub rate limit.
  2026-03-11k: Added self_sync_check() — V1/V2/V6 fix.
               Runs on startup + after every apply_evolution().
               Reads CORE_SELF.md _last_updated, compares to session count delta.
               If CORE_SELF.md stale (>7d with active sessions) → Telegram warning to owner.
               Evolution hook: structural evolutions (schema/tool/architecture/file)
               require core_self_updated flag in diff_content — else Telegram reminder sent.
"""
