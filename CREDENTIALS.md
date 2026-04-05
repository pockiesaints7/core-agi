# CREDENTIALS — core-agi
# DO NOT COMMIT ACTUAL VALUES — this file is a template only
# Copy to .env and fill in values from new Supabase project

## Supabase (NEW PROJECT)
SUPABASE_URL=https://<NEW_PROJECT_REF>.supabase.co
SUPABASE_SERVICE_KEY=<service_role_key>
SUPABASE_SERVICE_ROLE_KEY=<service_role_key>
SUPABASE_ANON_KEY=<anon_key>
SUPABASE_REF=<new_project_ref>
SUPABASE_PAT=<personal_access_token>
SUPABASE_DIRECT_URL=postgresql://postgres:<db_password>@db.<new_project_ref>.supabase.co:5432/postgres
NEXT_PUBLIC_SUPABASE_URL=https://<NEW_PROJECT_REF>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon_key>

## Telegram
TELEGRAM_BOT_TOKEN=<bot_token>
TELEGRAM_OWNER_ID=<owner_chat_id>
TELEGRAM_WEBHOOK_SECRET=<webhook_secret>

## AI / LLM
GROQ_API_KEY=<groq_key>
GEMINI_API_KEY=<gemini_key>
OPENROUTER_API_KEY=<openrouter_key>
VOYAGE_API_KEY=<voyage_key>

## GitHub
GITHUB_TOKEN=<github_pat>
GITHUB_REPO=pockiesaints7/core-agi

## MCP
MCP_SECRET=<mcp_secret>
PORT=8081

## Egress Guard (optional overrides)
# EGRESS_GUARD_DISABLED=0
# EGRESS_GLOBAL_MAX_PER_HOUR=600
# EGRESS_TABLE_MAX_PER_HOUR=120
# EGRESS_CACHE_TTL_S=120

## Research intervals (optional overrides)
# RESEARCH_INTERVAL_S=14400
# PROACTIVE_INTERVAL_S=14400

## Feature flags
CORE_REPO_MAP_ENABLED=false
CORE_SEMANTIC_PROJECTION_ENABLED=true
CORE_SEMANTIC_PROJECTION_INTERVAL_S=3600
CORE_SEMANTIC_PROJECTION_BATCH_LIMIT=5
