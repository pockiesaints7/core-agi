# CORE GOD MODE — Agentic Tool Improvement Master Plan v1.0

**Created:** 2026-03-13  
**Author:** Claude Desktop session  
**Status:** PENDING EXECUTION  
**Priority order:** P5 → P4 → P3 → P2

---

## WHY THIS EXISTS — ROOT CAUSES OF SLOWNESS

Observed this session (2026-03-13):

1. **core.py 136KB** — GitHub `/contents/` API silently fails near 1MB; `gh_search_replace` uses this API = unreliable on large files
2. **No function finder** — locating any function requires 3–5 `gh_read_lines` binary-search calls
3. **Every edit = full file fetch** — 6 patches = 6 separate full file fetches + writes
4. **No atomic patch+deploy+wait** — always 3+ separate tool calls minimum
5. **Indentation whitespace bugs** — `old_str` matching fails silently on indent differences
6. **No live stdout logs** — blind to what's actually running in Railway
7. **claude.ai (web) has NO safe edit path** — `create_or_update_file` truncates, `gh_search_replace` unreliable at size

---

## CATEGORY 1: CRITICAL BUG FIXES (P5 — DO FIRST, unblocks everything)

### 1A. `gh_search_replace` → Git Blobs API
- **Problem:** Uses `/contents/` API → fails silently on large files
- **Fix:** `GET /repos/{repo}/git/blobs/{sha}` with `Accept: application/vnd.github.v3.raw`
- **Write path:** create blob → new tree → new commit → update ref (same as PS scripts)
- **Impact:** ALL surgical edits become reliable forever regardless of file size

### 1B. `multi_patch` → Git Blobs API  
- Same fix as 1A — already written but broken on large files
- One fetch, N replacements, one atomic write

### 1C. `t_gh_read_lines` → Git Blobs API
- Preemptive fix — currently works but will break as core.py grows
- Add `_gh_blob_read(path, repo)` helper used by all 3 tools

### Implementation helper to add to core.py:
```python
def _gh_blob_read(path, repo=None):
    """Read file via Git Blobs API — no size limit, works for any file."""
    repo = repo or GITHUB_REPO
    h = _ghh()
    # Get current tree
    ref = httpx.get(f"https://api.github.com/repos/{repo}/git/ref/heads/main", headers=h, timeout=10)
    ref.raise_for_status()
    commit = httpx.get(f"https://api.github.com/repos/{repo}/git/commits/{ref.json()['object']['sha']}", headers=h, timeout=10)
    tree = httpx.get(f"https://api.github.com/repos/{repo}/git/trees/{commit.json()['tree']['sha']}", headers=h, timeout=10)
    blob = next((f for f in tree.json()["tree"] if f["path"] == path), None)
    if not blob: raise FileNotFoundError(f"{path} not found in repo")
    r = httpx.get(f"https://api.github.com/repos/{repo}/git/blobs/{blob['sha']}",
                  headers={**h, "Accept": "application/vnd.github.v3.raw"}, timeout=30)
    r.raise_for_status()
    return r.text, blob["sha"]

def _gh_blob_write(path, content, message, repo=None):
    """Write file via Git Trees API — atomic, no size limit."""
    repo = repo or GITHUB_REPO
    h = _ghh()
    ref = httpx.get(f"https://api.github.com/repos/{repo}/git/ref/heads/main", headers=h, timeout=10)
    ref.raise_for_status()
    current_sha = ref.json()["object"]["sha"]
    commit = httpx.get(f"https://api.github.com/repos/{repo}/git/commits/{current_sha}", headers=h, timeout=10)
    tree_sha = commit.json()["tree"]["sha"]
    # Create blob
    blob_r = httpx.post(f"https://api.github.com/repos/{repo}/git/blobs", headers=h,
                        json={"content": content, "encoding": "utf-8"}, timeout=30)
    blob_r.raise_for_status()
    new_blob_sha = blob_r.json()["sha"]
    # Create new tree
    tree_r = httpx.post(f"https://api.github.com/repos/{repo}/git/trees", headers=h,
                        json={"base_tree": tree_sha, "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": new_blob_sha}]}, timeout=20)
    tree_r.raise_for_status()
    new_tree_sha = tree_r.json()["sha"]
    # Create commit
    commit_r = httpx.post(f"https://api.github.com/repos/{repo}/git/commits", headers=h,
                          json={"message": message, "tree": new_tree_sha, "parents": [current_sha]}, timeout=15)
    commit_r.raise_for_status()
    new_commit_sha = commit_r.json()["sha"]
    # Update ref
    httpx.patch(f"https://api.github.com/repos/{repo}/git/refs/heads/main", headers=h,
                json={"sha": new_commit_sha}, timeout=15).raise_for_status()
    return new_commit_sha
```

---

## CATEGORY 2: NEW POWER TOOLS (P4)

### 2A. `t_core_py_edit(patches, message)` — Dedicated core.py editor
- Hardcoded to core.py, uses `_gh_blob_read` + `_gh_blob_write` always
- Accepts `patches` = JSON array of `{old_str, new_str}`
- Pre-checks: verify all old_str exist and are unambiguous before writing
- Post-checks: basic syntax validation (see 2C)
- Auto-logs to mistakes table if any patch fails
- Returns: `{ok, applied, skipped, new_line_count, commit_sha}`

### 2B. `t_core_py_fn(fn_name)` — Read single function by name
- Input: function name string e.g. `"t_redeploy"`
- Fetches core.py via blob API, finds `def {fn_name}(`, reads until next `def ` at same indent level
- Returns: `{ok, fn_name, start_line, end_line, source}`
- **This was the #1 bottleneck** — used ~20 times this session as binary gh_read_lines searches
- One call replaces 3-5 calls

### 2C. `t_core_py_validate()` — Pre-deploy syntax check
- Fetch core.py, run checks:
  - `def` count vs indented block balance (rough check)
  - No `def def` or `import import` double keywords (past incidents)
  - TOOLS dict has exactly one closing `}`
  - All `"fn": t_*` references in TOOLS dict exist as functions in the file
  - No `RAILWAY_TOKEN` or `backboard.railway` references (regression)
  - File size < 200KB warning
- Returns: `{ok, errors[], warnings[], line_count, size_kb}`
- **Run automatically before any deploy**

### 2D. `t_session_start()` — One-call session bootstrap
- Bundles: `get_state` + `get_system_health` + `search_mistakes(limit=5)` + `check_evolutions(limit=5)`
- Returns: `{health, counts, step, last_session, recent_mistakes, pending_evolutions}`
- Saves 3-4 tool calls at the start of **every single session**

### 2E. `t_session_end(summary, actions, domain, patterns, quality)` — One-call session close
- Bundles: `sb_insert(sessions)` + `reflect()` + optionally updates SESSION.md
- Detects step-change keywords in summary → auto-updates SESSION.md
- Session data currently gets lost because multi-step close is tedious
- Returns: `{session_id, reflection_id, session_md_updated}`

### 2F. `t_core_py_rollback(commit_sha)` — Emergency restore
- Input: any commit SHA (short or full)
- Fetches core.py at that commit, writes back as new commit on main
- Triggers redeploy automatically
- Returns: `{ok, restored_from, new_commit, redeploying}`
- Currently takes 5+ manual steps — must be instant in emergencies

### 2G. `t_sb_bulk_insert(table, rows_json)` — Multi-row insert
- Current `sb_insert` does 1 row at a time
- Supabase PostgREST supports array inserts: `POST /rest/v1/{table}` with JSON array body
- Use case: insert 10 KB entries, 5 backlog items in one call

### 2H. `t_diff(path, sha_a, sha_b)` — Compare two commits
- Fetches file at both commits, returns unified diff
- Use case: "what exactly changed in last 3 patches?" — currently impossible without GitHub UI

---

## CATEGORY 3: IMPROVE EXISTING TOOLS (P3)

| Tool | Improvement |
|------|-------------|
| `search_in_file` | Add `regex=false` + `case_sensitive=false` params |
| `gh_search_replace` | Add `dry_run=true` mode — show what WOULD change without committing |
| `deploy_and_wait` | Send Telegram ping when deploy completes (async notify) |
| `build_status` | Show 5 commits (not 3) + `time_since` human readable field |
| `get_backlog` | Add `type` filter param (new_tool/logic_improvement/etc.) |
| `logs` | Add `keyword` filter to search commit messages |

---

## CATEGORY 4: DELETE / DEPRECATE (P2)

| Item | Action | Reason |
|------|--------|--------|
| `route` tool | DEPRECATE | Telegram free-text removed; route engine no longer needed |
| `write_file` | ADD GUARD: block core.py | Full overwrite = corruption. Past incident. |
| `queue_poller` auto-exec | REMOVE execution, keep notify-only | Runs tasks without owner — dangerous |

---

## CATEGORY 5: ARCHITECTURE (P2 — biggest long-term fix)

### 5A. Split core.py into modules (~50KB each)
```
core_main.py    — FastAPI app, MCP dispatcher, webhook, startup
core_tools.py   — all t_* MCP tool functions
core_train.py   — training pipeline (cold_processor, background_researcher, kb_mining)
core_github.py  — all GitHub/Git API helpers (_gh_blob_read, _gh_blob_write, etc.)
```
- Each file stays under 50KB permanently
- No more GitHub API size issues ever
- Easier to audit, edit, and understand

### 5B. Expose /patch endpoint to claude.ai
- Add `MCP_SECRET` to the MCP startup context payload
- claude.ai sessions can use `POST /patch` for surgical edits
- Gives claude.ai a safe edit path without full-file risks

### 5C. Centralize `_gh_blob_read` / `_gh_blob_write`
- One implementation, all tools use it
- Automatic large-file support for everything

---

## EXECUTION ORDER

```
Session N+1:  P5 — Fix 1A+1B+1C (Blobs API) — unblocks all future edits
Session N+2:  P4 — 2C validate + 2B fn + 2A editor
Session N+3:  P4 — 2D session_start + 2E session_end + 2F rollback
Session N+4:  P3 — Category 3 improvements
Session N+5:  P2 — Architecture split (5A) — biggest long-term
Ongoing:      P2 — Category 4 deprecations (do as you touch each area)
```

---

## TRACKING

- KB entry: `CORE GOD MODE — Agentic Tool Improvement Master Plan v1.0`
- Backlog items: registered with P5/P4/P3/P2 priorities
- This file: `GOD_MODE_PLAN.md` in repo root — CORE reads on startup via self_sync_check

---
*Last updated: 2026-03-13 by Claude Desktop session*
