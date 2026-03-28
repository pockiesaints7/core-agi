"""scraper/knowledge/sources/stackoverflow.py
Fetch Q&A pairs from Stack Exchange public API.
300 req/day without key. Set STACKOVERFLOW_KEY env var for 10k/day.
"""
import httpx
import os
from datetime import datetime, timezone
from ..scorer import compute_engagement_score

SO_API     = "https://api.stackexchange.com/2.3/search/advanced"
SO_ANSWERS = "https://api.stackexchange.com/2.3/questions/{ids}/answers"
TAGS       = "llm;ai-agent;langchain;large-language-model;openai-api"
MIN_ANSWER_SCORE = 5


def _ts_to_iso(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _strip_html(html: str) -> str:
    """Basic HTML tag stripping for SO answer bodies."""
    import re
    text = re.sub(r"<code>(.*?)</code>", lambda m: f"\n```\n{m.group(1)}\n```\n", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as html_lib
    text = html_lib.unescape(text)
    return " ".join(text.split())


def _is_relevant(title: str, body: str, topic_keywords: list) -> bool:
    combined = (title + " " + body).lower()
    return any(kw in combined for kw in topic_keywords)


async def fetch(topic: str, max_results: int = 30) -> list:
    """Fetch answered SO questions + top answers about topic.
    Returns list of RawSource dicts.
    """
    print(f"[SO] fetch: topic={topic} max={max_results}")
    topic_keywords = [kw.lower() for kw in topic.split()]
    api_key = os.environ.get("STACKOVERFLOW_KEY", "")
    results = []

    base_params = {
        "order":  "desc",
        "sort":   "votes",
        "q":      topic,
        "site":   "stackoverflow",
        "filter": "withbody",
        "tagged": TAGS,
        "pagesize": min(max_results, 30),
    }
    if api_key:
        base_params["key"] = api_key

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(SO_API, params=base_params, timeout=15)
            if r.status_code != 200:
                print(f"[SO] API error: {r.status_code}")
                return []

            data    = r.json()
            items   = data.get("items", [])
            print(f"[SO] Got {len(items)} questions")

            # Collect question IDs to batch-fetch answers
            q_ids   = [str(q["question_id"]) for q in items if q.get("is_answered")]
            answers_map = {}

            if q_ids:
                try:
                    ans_params = {"order": "desc", "sort": "votes", "site": "stackoverflow", "filter": "withbody", "pagesize": 5}
                    if api_key:
                        ans_params["key"] = api_key
                    ids_str = ";".join(q_ids[:20])
                    ra = await client.get(SO_ANSWERS.format(ids=ids_str), params=ans_params, timeout=15)
                    if ra.status_code == 200:
                        for ans in ra.json().get("items", []):
                            qid = ans.get("question_id")
                            if qid not in answers_map:
                                answers_map[qid] = []
                            if ans.get("score", 0) >= MIN_ANSWER_SCORE:
                                answers_map[qid].append(ans)
                except Exception as e:
                    print(f"[SO] Answers fetch error: {e}")

            for q in items:
                if len(results) >= max_results:
                    break

                title   = q.get("title", "")
                body    = _strip_html(q.get("body", "") or "")
                if not _is_relevant(title, body, topic_keywords):
                    continue

                q_id    = q.get("question_id")
                q_score = q.get("score", 0) or 0
                views   = q.get("view_count", 0) or 0
                created = q.get("creation_date", 0)

                # Build full Q&A content
                full_content = f"Q: {title}\n\n{body}"
                questions_answered = [title]
                best_answer_score = 0

                q_answers = answers_map.get(q_id, [])
                accepted  = [a for a in q_answers if a.get("is_accepted")]
                top_voted = sorted(q_answers, key=lambda x: x.get("score", 0), reverse=True)

                for ans in (accepted + [a for a in top_voted if not a.get("is_accepted")])[:3]:
                    ans_text  = _strip_html(ans.get("body", "") or "")
                    ans_score = ans.get("score", 0) or 0
                    best_answer_score = max(best_answer_score, ans_score)
                    tag = "✓ Accepted" if ans.get("is_accepted") else f"Score: {ans_score}"
                    full_content += f"\n\nA [{tag}]: {ans_text[:1000]}"

                raw_engagement = {
                    "answer_score": best_answer_score,
                    "view_count":   views,
                    "is_accepted":  100 if q.get("accepted_answer_id") else 0,
                }
                engagement_score = compute_engagement_score(raw_engagement, "stackoverflow")

                results.append({
                    "url":             q.get("link", f"https://stackoverflow.com/q/{q_id}"),
                    "source_type":     "stackoverflow",
                    "source_platform": "stackoverflow.com",
                    "title":           title,
                    "author":          q.get("owner", {}).get("display_name", ""),
                    "published_at":    _ts_to_iso(created),
                    "content_hash":    str(q_id),
                    "engagement_score": engagement_score,
                    "raw_engagement":  raw_engagement,
                    "trust_level":     3,
                    "topics":          [topic],
                    "status":          "active",
                    "full_content":    full_content[:5000],
                    "summary":         body[:500],
                    "key_concepts":    [],
                    "questions_answered": questions_answered,
                    "consensus_level": "established" if q.get("accepted_answer_id") else "debated",
                })

        except Exception as e:
            print(f"[SO] Error: {e}")

    print(f"[SO] Returning {len(results)} sources")
    return results
