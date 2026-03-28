export default {
  async fetch(request, env) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    const tok = parts[0] === "v2" ? parts[1] : parts[0];

    if (!tok || tok !== env.VAULT_TOKEN) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
      });
    }

    // Mirrors core.py os.environ reads exactly — nothing more, nothing less
    const config = {
      version: "5.0",
      service: "CORE v5.0 Step 0",

      // Required — core.py crashes without these
      GROQ_API_KEY:         env.GROQ_API_KEY,
      SUPABASE_URL:         env.SUPABASE_URL,
      SUPABASE_SERVICE_KEY: env.SUPABASE_SERVICE_KEY,
      SUPABASE_ANON_KEY:    env.SUPABASE_ANON_KEY,
      TELEGRAM_BOT_TOKEN:   env.TELEGRAM_BOT_TOKEN,
      TELEGRAM_CHAT_ID:     env.TELEGRAM_CHAT_ID,
      GITHUB_PAT:           env.GITHUB_PAT,
      MCP_SECRET:           env.MCP_SECRET,

      // Optional — have defaults in core.py
      GROQ_MODEL:           env.GROQ_MODEL,
      GROQ_MODEL_FAST:      env.GROQ_MODEL_FAST,
      GITHUB_USERNAME:      env.GITHUB_USERNAME,
      PORT:                 env.PORT,
    };

    return new Response(JSON.stringify(config, null, 2), {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store, no-cache, must-revalidate"
      }
    });
  }
};
