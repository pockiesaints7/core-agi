# HANDOFF — Fix `t_redeploy()` environmentId Bug

**Created:** 2026-03-13  
**Priority:** Medium — redeploy works via Railway auto-deploy on push, but `t_redeploy()` MCP tool always errors  
**Status:** Pending — do in next session  

---

## What Is Broken

`t_redeploy()` in `core.py` fails with:

```
{"ok": false, "error": "Field \"serviceInstanceRedeploy\" argument \"environmentId\" of type \"String!\" is required, but it was not provided."}
```

Railway GraphQL API v2 requires `environmentId` in the `serviceInstanceRedeploy` mutation. The current function only passes `serviceId`.

---

## Root Cause

Current broken code in `t_redeploy()`:

```python
query = "mutation($id:String!){serviceInstanceRedeploy(serviceId:$id)}"
r = httpx.post(
    "https://backboard.railway.app/graphql/v2",
    ...
    json={"query": query, "variables": {"id": service_id}},
)
```

Missing: `environmentId` argument in both the mutation signature and the variables.

---

## The Fix

Railway GraphQL mutation signature requires:
```graphql
mutation($sid: String!, $eid: String!) {
  serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
}
```

**All three IDs are already available as env var fallbacks in core.py:**
- `RAILWAY_TOKEN` → must be set in Railway dashboard env vars (already is)
- `RAILWAY_SERVICE_ID` → fallback: `"48ad55bd-6be2-4d8a-83df-34fc05facaa2"` (core-agi service)
- `RAILWAY_ENV_ID` → fallback: `"ff3f2a4c-4085-445e-88ff-a423862d00e8"` (production environment)

Also stored in `CREDENTIALS.md` at `C:\Users\rnvgg\.claude-skills\services\CREDENTIALS.md`.

---

## Exact Patch to Apply

Use `gh_search_replace` on `core.py`:

**old_str:**
```python
        query = "mutation($id:String!){serviceInstanceRedeploy(serviceId:$id)}"
        r = httpx.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": {"id": service_id}},
            timeout=15,
        )
```

**new_str:**
```python
        query = "mutation($sid:String!,$eid:String!){serviceInstanceRedeploy(serviceId:$sid,environmentId:$eid)}"
        r = httpx.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": {"sid": service_id, "eid": env_id}},
            timeout=15,
        )
```

Note: `env_id` is already fetched earlier in the function:
```python
env_id = os.environ.get("RAILWAY_ENV_ID", "ff3f2a4c-4085-445e-88ff-a423862d00e8")
```
So no new variable needed — just fix the query string and variables dict.

---

## After Patching

1. Railway will auto-deploy on push (no manual trigger needed to test)
2. Verify with: `core-agi:redeploy(reason="test fix")` → should return `{"ok": true}`
3. Delete this file after fix confirmed: `docs/HANDOFF_redeploy_fix.md`

---

## Context — What Else Happened This Session

- Added `review_evolutions` MCP tool (returns URL to `/review`)
- Added `GET /review` — full HTML interactive widget served from Railway
- Added `GET /api/evolutions` — JSON endpoint for widget to fetch pending evolutions
- Fixed `CREDENTIALS.md` — Railway Service ID and Environment ID were missing, now added
- Logged mistake: Claude was claiming credentials inaccessible without reading CREDENTIALS.md

## Key Credentials Reminder (for next Claude)

| Item | Value | Location |
|------|-------|----------|
| MCP_SECRET | `core_mcp_secret_2026_REINVAGNAR` | CREDENTIALS.md |
| Railway Token | `4c7595e7-9159-4e5d-9178-969c97447421` | CREDENTIALS.md |
| Railway Service ID | `48ad55bd-6be2-4d8a-83df-34fc05facaa2` | CREDENTIALS.md + core.py fallback |
| Railway Env ID | `ff3f2a4c-4085-445e-88ff-a423862d00e8` | CREDENTIALS.md + core.py fallback |
| /patch endpoint secret | same as MCP_SECRET above | CREDENTIALS.md |
