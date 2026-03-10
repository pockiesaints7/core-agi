"""
Jarvis Brain Scanner v4
========================
Two classes of analysis, five signal sources.

CLASS A — MAINTENANCE  : repair broken/stale/incomplete entries
CLASS B — GROWTH       : find patterns, gaps, synthesis opportunities

SIGNAL SOURCES:
  1. Supabase DB patterns       (direct SQL — always available)
  2. Session action vocabulary  (mine sessions.actions[] arrays)
  3. Cross-table coherence      (find contradictions between tables)
  4. PC manifest                (memory key pushed by boot — knows what's on PC)
  5. Knowledge citation decay   (KB entries never mentioned in recent sessions)
  6. Code template gaps         (pc_manifest + session actions = patterns without templates)

ARCHITECTURE NOTE:
  Railway cannot reach the PC filesystem directly.
  Instead, on every boot Jarvis pushes two memory keys:
    system/pc_manifest      → {skill_files: [...], memory_exports: [...], last_boot: "..."}
    system/brain_health_report → this scanner's output
  Scanner reads pc_manifest to reason about PC-side signals.
"""
import os, json, re, httpx
from datetime import datetime, timezone
from collections import Counter

SUPABASE_REF = os.environ.get("SUPABASE_REF", "qbfaplqiakwjvrtwpbmr")
SUPABASE_PAT = os.environ.get("SUPABASE_PAT")
JARVIS_SECRET = os.environ.get("JARVIS_SECRET")
API_BASE = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"
SELF_BASE = f"http://localhost:{os.environ.get('PORT', 8000)}"

async def sql(query: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(API_BASE,
            headers={"Authorization": f"Bearer {SUPABASE_PAT}", "Content-Type": "application/json"},
            json={"query": query})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "value" in data:
            return data["value"]
        return data if isinstance(data, list) else []

async def get_memory_key(key: str) -> dict | None:
    rows = await sql(f"SELECT value FROM memory WHERE key = '{key}' AND category = 'system'")
    if rows:
        try:
            return json.loads(rows[0]["value"])
        except Exception:
            return None
    return None

async def save_report(report: dict):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{SELF_BASE}/brain/memory",
            headers={"Authorization": f"Bearer {JARVIS_SECRET}", "Content-Type": "application/json"},
            json={"category": "system", "key": "brain_health_report", "value": json.dumps(report)})

# ═══════════════════════════════════════════════════════
# CLASS A — MAINTENANCE
# ═══════════════════════════════════════════════════════

async def check_stale_knowledge(flags):
    rows = await sql("""
        SELECT topic, domain, updated_at FROM knowledge_base
        WHERE confidence = 'medium'
        AND updated_at < NOW() - INTERVAL '30 days'
        ORDER BY updated_at ASC
    """)
    if rows:
        flags.append({
            "class": "maintenance", "type": "stale_knowledge_confidence", "severity": "low",
            "source": "database",
            "message": f"{len(rows)} KB entries stuck at 'medium' confidence for 30+ days.",
            "action": "Review each. Upgrade to 'high' if still accurate, delete if obsolete.",
            "topics": [r["topic"] for r in rows]
        })

async def check_incomplete_mistakes(flags):
    rows = await sql("""
        SELECT id, context, what_failed FROM mistakes
        WHERE correct_approach IS NULL OR correct_approach = ''
        ORDER BY id DESC
    """)
    if rows:
        flags.append({
            "class": "maintenance", "type": "incomplete_mistakes", "severity": "medium",
            "source": "database",
            "message": f"{len(rows)} mistakes have no correct_approach — recorded pain without the lesson.",
            "action": "Fetch each by ID, fill in correct_approach via Supabase SQL UPDATE.",
            "ids": [r["id"] for r in rows]
        })

async def check_session_gap(flags):
    rows = await sql("SELECT COUNT(*) as cnt FROM sessions WHERE created_at > NOW() - INTERVAL '7 days'")
    if rows and int(rows[0]["cnt"]) == 0:
        flags.append({
            "class": "maintenance", "type": "no_recent_sessions", "severity": "high",
            "source": "database",
            "message": "No sessions logged in 7 days. Jarvis may not be saving, or system is idle.",
            "action": "Verify session logging works. POST /brain/session at end of this session."
        })

async def check_stale_memory(flags):
    rows = await sql("""
        SELECT category, key, updated_at FROM memory
        WHERE updated_at < NOW() - INTERVAL '90 days'
        AND category != 'system'
        ORDER BY updated_at ASC
    """)
    if rows:
        flags.append({
            "class": "maintenance", "type": "stale_memory", "severity": "low",
            "source": "database",
            "message": f"{len(rows)} memory entries untouched for 90+ days. Facts may have changed.",
            "action": "Review each. Update if changed, delete if no longer relevant.",
            "keys": [f"{r['category']}/{r['key']}" for r in rows]
        })

async def check_unabsorbed_exports(flags):
    """
    SOURCE: PC manifest (pushed to memory on boot).
    Detects memory export files that exist on PC but haven't been absorbed.
    """
    manifest = await get_memory_key("pc_manifest")
    if not manifest:
        return  # Manifest not yet pushed — first boot or old Jarvis version

    exports = manifest.get("memory_exports", [])
    absorbed = manifest.get("absorbed_exports", [])
    unabsorbed = [f for f in exports if f not in absorbed and f != "README.md"]

    if unabsorbed:
        flags.append({
            "class": "maintenance", "type": "unabsorbed_exports", "severity": "medium",
            "source": "pc_manifest",
            "message": f"{len(unabsorbed)} memory export file(s) on PC have never been absorbed into brain.",
            "action": "On next boot: run ROUTINE [4] for each unabsorbed file. Update pc_manifest.absorbed_exports after.",
            "files": unabsorbed
        })

async def check_skill_file_drift(flags):
    """
    SOURCE: PC manifest.
    If skill files haven't been updated in a long time but sessions show
    active system evolution, the docs are probably stale.
    """
    manifest = await get_memory_key("pc_manifest")
    if not manifest:
        return

    skill_files = manifest.get("skill_files", {})
    # Get last session that mentioned architecture/system work
    arch_sessions = await sql("""
        SELECT created_at FROM sessions
        WHERE 'code_push' = ANY(actions)
           OR 'jarvis_prompt_update' = ANY(actions)
           OR 'schema_change' = ANY(actions)
        ORDER BY created_at DESC LIMIT 1
    """)
    if not arch_sessions:
        return

    last_arch = arch_sessions[0]["created_at"]
    stale_files = []
    for fname, last_modified in skill_files.items():
        if last_modified and last_modified < last_arch:
            stale_files.append(fname)

    if stale_files:
        flags.append({
            "class": "maintenance", "type": "skill_file_drift", "severity": "low",
            "source": "pc_manifest",
            "message": f"Skill files may be behind recent system changes: {stale_files}",
            "action": "Review each file against recent code pushes and schema changes. Update sections that are outdated.",
            "files": stale_files
        })


# ═══════════════════════════════════════════════════════
# CLASS B — GROWTH
# ═══════════════════════════════════════════════════════

async def check_mistake_patterns(flags):
    """SOURCE: DB. Recurring failure tags = pattern worth a playbook entry."""
    rows = await sql("SELECT tags FROM mistakes ORDER BY id DESC LIMIT 60")
    tag_counts = Counter(t for r in rows for t in (r.get("tags") or []))
    recurring = {t: c for t, c in tag_counts.items() if c >= 3}
    if recurring:
        flags.append({
            "class": "growth", "type": "recurring_mistake_pattern", "severity": "medium",
            "source": "database",
            "message": f"Recurring failure patterns: {recurring}. These deserve a definitive playbook entry.",
            "action": "Synthesize a playbook entry from the pattern. What is the root cause? What is the definitive correct method? POST /brain/playbook.",
            "recurring_tags": recurring
        })

async def check_session_action_vocabulary(flags):
    """
    SOURCE: Session actions[].
    Domains Jarvis keeps working in but has no KB entries for.
    This catches growth areas from ACTUAL WORK DONE, not just mistakes.
    """
    rows = await sql("SELECT actions FROM sessions ORDER BY created_at DESC LIMIT 30")
    if not rows:
        return

    # Collect all action tags from recent sessions
    action_counts = Counter(a for r in rows for a in (r.get("actions") or []))

    # Remove meta-actions, keep domain-like ones
    meta = {"brain_load", "health_check", "memory_import", "code_push", "schema_change",
            "jarvis_prompt_update", "jarvis_os_md_update", "skill_md_update",
            "kb_save", "playbook_save", "session_logged"}
    domain_actions = {a: c for a, c in action_counts.items()
                      if a not in meta and c >= 2 and len(a) > 4}

    # Check which of these have KB coverage
    if not domain_actions:
        return

    kb_rows = await sql("SELECT topic, domain FROM knowledge_base")
    # Build coverage set: topic slugs in underscore + space form, domains, and partial prefixes
    # Fixes: action "pc_manifest_push" was not matching topic "pc_manifest_push_workflow"
    # because old code searched space-form "pc manifest push" in underscore-joined kb_text
    kb_topics_set = set()
    for r in kb_rows:
        t = r["topic"].lower()
        d = r["domain"].lower()
        kb_topics_set.add(t)
        kb_topics_set.add(t.replace("_", " "))
        kb_topics_set.add(t.replace("-", "_"))
        kb_topics_set.add(d)
        kb_topics_set.add(d.replace("-", "_"))
        # Add 2-word prefix of topic for partial action matching
        parts = t.replace("-", "_").split("_")
        if len(parts) >= 2:
            kb_topics_set.add("_".join(parts[:2]))
            kb_topics_set.add(" ".join(parts[:2]))

    uncovered = {}
    for action, count in domain_actions.items():
        a_lower = action.lower()
        a_spaced = a_lower.replace("_", " ")
        # Covered if: exact match, space form match, or action is a prefix of any KB topic
        covered = (
            a_lower in kb_topics_set or
            a_spaced in kb_topics_set or
            any(t.startswith(a_lower) for t in kb_topics_set)
        )
        if not covered:
            uncovered[action] = count

    if uncovered:
        flags.append({
            "class": "growth", "type": "active_domain_no_knowledge", "severity": "medium",
            "source": "session_actions",
            "message": f"Jarvis keeps working in these areas but has no KB entries for them: {uncovered}. Working without structured knowledge.",
            "action": "Write KB entries for each uncovered domain. Capture what Jarvis already knows from practice.",
            "uncovered_domains": uncovered
        })

async def check_cross_table_contradiction(flags):
    """
    SOURCE: DB cross-table.
    Playbook says 'do X'. A mistake says 'doing X failed'.
    That's a contradiction — one of them is wrong.
    """
    playbook_rows = await sql("SELECT topic, method FROM playbook")
    mistake_rows = await sql("SELECT what_failed, correct_approach FROM mistakes")

    if not playbook_rows or not mistake_rows:
        return

    # Stop-words that carry no contradiction signal when shared
    stop_words = {
        "jarvis", "brain", "write", "call", "using", "after", "before", "session",
        "should", "always", "every", "never", "batch", "single", "brain", "table",
        "entry", "error", "check", "value", "query", "token", "items", "calls",
        "result", "output", "return", "table", "field", "where", "order", "limit",
        "saves", "actions", "memory", "format", "method", "approach", "pattern",
        "correct", "failed", "current", "content", "provide", "example", "update",
        "version", "previous", "conflict", "target", "constraint", "require"
    }
    contradictions = []
    for pb in playbook_rows:
        pb_topic = pb["topic"].lower().replace("_", " ")
        method_words = set(re.findall(r"\b\w{5,}\b", pb["method"].lower())) - stop_words
        for mk in mistake_rows:
            failed_text = mk["what_failed"].lower()
            failed_words = set(re.findall(r"\b\w{5,}\b", failed_text)) - stop_words
            overlap = method_words & failed_words
            # Real contradiction signal: topic appears in the failure AND significant overlap
            # Avoids false positives from shared domain vocabulary
            topic_words = [w for w in pb_topic.split() if len(w) > 4]
            topic_in_failure = len(topic_words) > 0 and all(w in failed_text for w in topic_words)
            if topic_in_failure and len(overlap) >= 6:
                contradictions.append({
                    "playbook_topic": pb["topic"],
                    "shared_words": list(overlap)[:5]
                })

    if contradictions:
        flags.append({
            "class": "growth", "type": "cross_table_contradiction", "severity": "high",
            "source": "cross_table",
            "message": f"{len(contradictions)} possible contradictions: playbook says do X, but a mistake says doing X failed.",
            "action": "Review each. One is wrong — either the playbook is outdated, or the mistake was context-specific. Reconcile and update whichever is wrong.",
            "contradictions": contradictions[:5]  # top 5
        })

async def check_knowledge_never_referenced(flags):
    """
    SOURCE: DB cross-table (KB vs sessions).
    KB entries that appear in zero recent session summaries may be dead weight,
    OR they're valuable but Jarvis forgot to use them.
    """
    kb_rows = await sql("""
        SELECT topic, domain, confidence, updated_at FROM knowledge_base
        WHERE updated_at < NOW() - INTERVAL '45 days'
        ORDER BY updated_at ASC
        LIMIT 20
    """)
    if not kb_rows:
        return

    session_rows = await sql("""
        SELECT summary FROM sessions
        ORDER BY created_at DESC LIMIT 20
    """)
    all_summaries = " ".join(r["summary"] or "" for r in session_rows).lower()

    never_used = []
    for kb in kb_rows:
        topic_words = kb["topic"].replace("_", " ").lower()
        # Check if topic appears anywhere in recent summaries
        if topic_words not in all_summaries and kb["domain"] not in all_summaries:
            never_used.append(kb["topic"])

    if len(never_used) >= 3:
        flags.append({
            "class": "growth", "type": "knowledge_citation_decay", "severity": "low",
            "source": "cross_table",
            "message": f"{len(never_used)} KB entries haven't appeared in recent sessions. Either Jarvis forgot about them, or they're outdated.",
            "action": "Review each. If still relevant: make a note to actively use this knowledge. If outdated: update or delete.",
            "topics": never_used[:10]
        })

async def check_session_complexity_growth(flags):
    """
    SOURCE: Session history.
    Are sessions getting more complex over time (more actions, richer summaries)?
    If not — Jarvis may be doing the same work repeatedly without growing.
    """
    rows = await sql("""
        SELECT
            array_length(actions, 1) as action_count,
            length(summary) as summary_len,
            created_at
        FROM sessions
        WHERE actions IS NOT NULL
        ORDER BY created_at ASC
    """)
    if len(rows) < 6:
        return  # Not enough history

    first_half = rows[:len(rows)//2]
    second_half = rows[len(rows)//2:]

    avg_actions_early = sum(r["action_count"] or 0 for r in first_half) / len(first_half)
    avg_actions_late  = sum(r["action_count"] or 0 for r in second_half) / len(second_half)
    avg_summary_early = sum(r["summary_len"] or 0 for r in first_half) / len(first_half)
    avg_summary_late  = sum(r["summary_len"] or 0 for r in second_half) / len(second_half)

    # If actions and summary depth are FLAT or DECLINING — no growth signal
    action_growth = (avg_actions_late - avg_actions_early) / max(avg_actions_early, 1)
    summary_growth = (avg_summary_late - avg_summary_early) / max(avg_summary_early, 1)

    if action_growth < 0.1 and summary_growth < 0.1:
        flags.append({
            "class": "growth", "type": "session_complexity_flat", "severity": "low",
            "source": "session_history",
            "message": f"Session complexity is flat or declining. Early avg actions: {avg_actions_early:.1f}, recent: {avg_actions_late:.1f}. Jarvis may be doing repetitive work without expanding capability.",
            "action": "Deliberately take on one harder task this session. Push into a domain not yet covered in KB.",
            "action_trend": round(action_growth * 100, 1),
            "summary_trend": round(summary_growth * 100, 1)
        })

async def check_synthesis_opportunity(flags):
    """SOURCE: DB. Domains with 5+ KB entries but no master overview."""
    rows = await sql("""
        SELECT domain, COUNT(*) as cnt FROM knowledge_base
        GROUP BY domain HAVING COUNT(*) >= 5 ORDER BY cnt DESC
    """)
    if not rows:
        return

    for r in rows:
        domain = r["domain"]
        synthesis = await sql(f"""
            SELECT topic FROM knowledge_base WHERE domain = '{domain}'
            AND (topic LIKE '%overview%' OR topic LIKE '%summary%'
              OR topic LIKE '%master%' OR topic LIKE '%guide%'
              OR topic LIKE '%principles%')
        """)
        if not synthesis:
            flags.append({
                "class": "growth", "type": "synthesis_opportunity", "severity": "low",
                "source": "database",
                "message": f"Domain '{domain}' has {r['cnt']} KB entries but no synthesis. Scattered knowledge < structured knowledge.",
                "action": f"Write topic='{domain}_master_guide'. Pull key insights from all {r['cnt']} entries into one coherent reference.",
                "domain": domain, "entry_count": r["cnt"]
            })

async def check_proven_upgrade_candidates(flags):
    """SOURCE: DB. High-confidence entries 60+ days old — they've earned 'proven'."""
    rows = await sql("""
        SELECT topic, domain, updated_at FROM knowledge_base
        WHERE confidence = 'high'
        AND updated_at < NOW() - INTERVAL '60 days'
        ORDER BY updated_at ASC LIMIT 10
    """)
    if rows:
        flags.append({
            "class": "growth", "type": "proven_upgrade_candidates", "severity": "low",
            "source": "database",
            "message": f"{len(rows)} KB entries have been 'high' confidence for 60+ days. If they've held up, upgrade to 'proven'.",
            "action": "For each: verify still accurate. POST /brain/knowledge with confidence='proven'.",
            "topics": [r["topic"] for r in rows]
        })

async def check_knowledge_density(flags, counts):
    """SOURCE: DB counts. Mistakes growing faster than knowledge = pain without wisdom."""
    mistakes = counts.get("mistakes", 0)
    kb = counts.get("knowledge_base", 0)
    if kb > 0 and mistakes / kb > 1.5:
        flags.append({
            "class": "growth", "type": "mistakes_outpacing_knowledge", "severity": "medium",
            "source": "database",
            "message": f"Mistakes ({mistakes}) are {mistakes/kb:.1f}x the KB ({kb}). Recording failures faster than building wisdom.",
            "action": "Find the 3 most repeated mistake tag clusters. Write a KB entry for each. Convert pain into structure.",
            "ratio": round(mistakes / kb, 2)
        })

async def check_playbook_never_evolved(flags):
    """SOURCE: DB. Methods at v1 for 60+ days — never improved."""
    rows = await sql("""
        SELECT topic, updated_at FROM playbook
        WHERE version = 1 AND updated_at < NOW() - INTERVAL '60 days'
        ORDER BY updated_at ASC
    """)
    if rows:
        flags.append({
            "class": "growth", "type": "playbook_never_evolved", "severity": "low",
            "source": "database",
            "message": f"{len(rows)} playbook entries have never been improved since creation (v1, 60+ days).",
            "action": "For each: reflect honestly. Has a better way been found in practice? Update or deepen why_best.",
            "topics": [r["topic"] for r in rows]
        })

async def check_knowledge_domain_gap(flags):
    """SOURCE: DB cross-table. Mistake tags with zero KB coverage (domain, topic, or keyword).
    
    FIX 2026-03-08: Original checked tags vs domain names only (apples vs oranges).
    Now checks tags against full KB coverage: domains + topics + individual topic words.
    Also expanded ignore list to cover core system operation tags that are intentionally
    documented inside broader KB entries (boot, session, etc. are covered in master guides).
    """
    kb_rows = await sql("SELECT DISTINCT domain, topic FROM knowledge_base")
    # Full coverage: domain names + topic slugs + individual meaningful words from topics
    kb_coverage = set()
    for r in kb_rows:
        d = r["domain"].lower().replace("-", "_")
        t = r["topic"].lower()
        kb_coverage.add(d)
        kb_coverage.add(t)
        kb_coverage.add(t.replace("_", " "))
        for word in t.replace("-", "_").split("_"):
            if len(word) > 4:
                kb_coverage.add(word)

    mistake_tags = {r["tag"] for r in await sql("SELECT DISTINCT unnest(tags) as tag FROM mistakes") if r.get("tag")}
    # Core system operation tags -- covered inside master guides, not standalone gaps
    ignore = {
        "jarvis", "brain", "api", "fix", "bug", "error", "railway", "supabase", "system",
        "session", "boot", "actions", "token", "batching", "efficiency", "session_end",
        "brain_write", "playbook", "knowledge", "memory", "sessions", "interface",
        "architecture", "compacted_transcript", "brain_first", "context", "engineering"
    }
    true_gaps = set()
    for tag in mistake_tags:
        if tag in ignore:
            continue
        tag_lower = tag.lower()
        # Only flag if tag has zero presence in KB coverage (not even as a substring of a topic word)
        if tag_lower not in kb_coverage and not any(tag_lower in c for c in kb_coverage):
            true_gaps.add(tag)

    if true_gaps:
        flags.append({
            "class": "growth", "type": "knowledge_domain_gap", "severity": "medium",
            "source": "cross_table",
            "message": f"Mistakes exist in these domains but no KB entries: {true_gaps}. Failing without building knowledge.",
            "action": "Write a KB entry for each gap domain. Capture what you know and what's been learned from mistakes.",
            "gap_domains": sorted(true_gaps)
        })


# ═══════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════


async def check_code_template_candidates(flags):
    """
    SOURCE: PC manifest + session actions + mistakes.
    Detects recurring patterns that have no code template yet in code-templates/.

    Logic:
    1. Read pc_manifest for list of existing templates (pushed on boot).
    2. Mine session actions (last 40) for patterns appearing 3+ times.
    3. Mine mistake tags (all time) for patterns appearing 3+ times.
    4. Cross-reference against known template coverage map.
    5. Anything uncovered = candidate for a new template.
    """
    manifest = await get_memory_key("pc_manifest")
    known_templates = set()
    if manifest:
        known_templates = set(manifest.get("code_templates", []))
    if not known_templates:
        # Baseline — matches code-templates/ library built 2026-03-08
        known_templates = {
            "jarvis_api", "github_push", "railway_deploy",
            "supabase_sql", "zip_reader", "wsl_runner"
        }

    session_rows = await sql("SELECT actions FROM sessions ORDER BY created_at DESC LIMIT 40")
    action_counts = Counter(a for r in session_rows for a in (r.get("actions") or []))

    mistake_rows = await sql("SELECT tags FROM mistakes ORDER BY id DESC LIMIT 100")
    tag_counts = Counter(t for r in mistake_rows for t in (r.get("tags") or []))

    candidate_scores = {}
    for p, c in {**action_counts, **tag_counts}.items():
        if c >= 3:
            candidate_scores[p] = candidate_scores.get(p, 0) + c

    meta_noise = {
        "boot", "brain_load", "session_logged", "memory_import", "kb_save",
        "playbook_save", "code_push", "schema_change", "fix", "bug", "error",
        "jarvis_prompt_update", "absorption", "pending_task_saved",
        "growth_flags_complete", "pc_manifest_push", "jarvis_os_md_update",
        "skill_md_update", "session_end_save", "contradiction_reconcile"
    }
    template_coverage = {
        "jarvis_api":      {"brain", "api", "memory", "knowledge", "playbook", "session_end", "brain_write"},
        "github_push":     {"github", "push", "commit", "repo", "sha"},
        "railway_deploy":  {"railway", "deploy", "health", "redeploy"},
        "supabase_sql":    {"supabase", "sql", "database", "schema", "alter"},
        "zip_reader":      {"zip", "absorption", "memory_import", "conversations", "export"},
        "wsl_runner":      {"wsl", "python"},
    }
    all_covered = set(t for tags in template_coverage.values() for t in tags)
    all_covered |= known_templates | meta_noise

    uncovered = {
        p: s for p, s in candidate_scores.items()
        if p.lower() not in all_covered and len(p) > 4
    }

    if uncovered:
        top = sorted(uncovered.items(), key=lambda x: x[1], reverse=True)[:5]
        flags.append({
            "class": "growth",
            "type": "code_template_candidate",
            "severity": "low",
            "source": "session_actions+mistakes+pc_manifest",
            "message": (
                f"{len(uncovered)} recurring patterns have no code template: "
                f"{dict(top)}. Being written from scratch each session."
            ),
            "action": (
                "For each: write a .ps1 template in "
                "C:\\Users\\rnvgg\\.claude-skills\\code-templates\\. "
                "Pattern: dot-source, named functions, Write-Host loaded msg. "
                "Add to README.md. Save playbook entry topic='code_template_NAME'. "
                "Push updated pc_manifest.code_templates list on next boot."
            ),
            "candidates": dict(top)
        })

async def run_scan() -> dict:
    now = datetime.now(timezone.utc)
    maintenance_flags = []
    growth_flags = []

    # Brain size counts
    counts = {}
    for table in ["memory", "knowledge_base", "mistakes", "playbook", "sessions"]:
        result = await sql(f"SELECT COUNT(*) as cnt FROM {table}")
        counts[table] = int(result[0]["cnt"]) if result else 0

    # ── CLASS A: MAINTENANCE ─────────────────────────────
    await check_stale_knowledge(maintenance_flags)
    await check_incomplete_mistakes(maintenance_flags)
    await check_session_gap(maintenance_flags)
    await check_stale_memory(maintenance_flags)
    await check_unabsorbed_exports(maintenance_flags)     # PC manifest source
    await check_skill_file_drift(maintenance_flags)       # PC manifest source

    # ── CLASS B: GROWTH ──────────────────────────────────
    await check_mistake_patterns(growth_flags)
    await check_session_action_vocabulary(growth_flags)   # session actions source
    await check_cross_table_contradiction(growth_flags)   # cross-table source
    await check_knowledge_never_referenced(growth_flags)  # cross-table source
    await check_session_complexity_growth(growth_flags)   # session history source
    await check_synthesis_opportunity(growth_flags)
    await check_proven_upgrade_candidates(growth_flags)
    await check_knowledge_density(growth_flags, counts)
    await check_playbook_never_evolved(growth_flags)
    await check_knowledge_domain_gap(growth_flags)
    await check_code_template_candidates(growth_flags)  # pc_manifest + session_actions source

    high   = len([f for f in maintenance_flags + growth_flags if f["severity"] == "high"])
    medium = len([f for f in maintenance_flags + growth_flags if f["severity"] == "medium"])

    return {
        "scanned_at": now.isoformat(),
        "brain_counts": counts,
        "maintenance_flags": maintenance_flags,
        "growth_flags": growth_flags,
        "summary": {
            "maintenance": len(maintenance_flags),
            "growth": len(growth_flags),
            "high_priority": high,
            "medium_priority": medium,
        },
        "needs_attention": (high + medium) > 0,
        "has_growth_opportunities": len(growth_flags) > 0,
    }




