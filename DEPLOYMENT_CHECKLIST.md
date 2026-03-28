# Orchestrator Deployment Checklist

## Phase 1: Local Testing (DO THIS FIRST)

1. **Extract files:**
   ```bash
   tar -xzf orchestrator_fixed.tar.gz
   cd orchestrator/
   ```

2. **Create .env file:**
   ```bash
   TELEGRAM_TOKEN=your_token
   TELEGRAM_CHAT=838737537
   SUPABASE_URL=your_url
   SUPABASE_KEY=your_key
   GROQ_API_KEY=your_key
   ```

3. **Test locally:**
   ```bash
   python3 test_orchestrator.py
   ```

4. **Expected output:**
   - Test 1: Message flows L0→L1→L2→...→L10
   - Test 2: Command execution with tools
   - Test 3: Security gate rejects anonymous

## Phase 2: Integration with core.py

1. **Replace mocks with real functions:**
   - In L2: `from core_config import sb_query, search_kb`
   - In L3/L4/L7/L9: `from core_config import groq_chat`
   - In L10: `from core_config import notify`

2. **Wire to core_main.py:**
   ```python
   from core_orch_layer1_fixed import layer_1_triage
   
   @app.post("/telegram")
   async def telegram_webhook(request: Request):
       update = await request.json()
       msg = await layer_1_triage(update, "telegram")
       return {"ok": True}
   ```

3. **Test with real Telegram:**
   - Send message to bot
   - Check Railway logs for L0→L10 flow
   - Verify response received

## Phase 3: Tool Integration

1. **Connect L5 to core_tools.py:**
   ```python
   # In layer 5
   from core_tools import TOOLS
   
   async def execute_tools(msg):
       for subtask in subtasks:
           tool_name = subtask.get("tool")
           if tool_name in TOOLS:
               # Call actual tool
               result = await TOOLS[tool_name]["fn"](...)
   ```

2. **Test tool execution:**
   - Try `/health` command
   - Try tool-requiring tasks
   - Verify tool results in msg.tool_results

## Phase 4: Evolution Integration

1. **Connect L7 to evolution queue:**
   ```python
   # In layer 7
   from core_train import add_evolution
   
   if analysis.get("propose_evolution"):
       await add_evolution(
           evolution_type="behavioral_rule",
           content=analysis.get("suggestion")
       )
   ```

## Phase 5: Deployment

1. **Push to GitHub:**
   ```bash
   git add core_orch_*.py orchestrator_message.py
   git commit -m "feat: Add functional 11-layer orchestrator"
   git push origin main
   ```

2. **Railway auto-deploys** - wait 35s

3. **Check health:**
   ```bash
   curl https://core-agi-production.up.railway.app/health
   ```

4. **Test via Telegram:**
   - Send test message
   - Check logs for full layer execution

## Rollback Plan

If orchestrator breaks existing functionality:

1. **Revert GitHub:**
   ```bash
   git revert HEAD
   git push origin main
   ```

2. **Railway auto-redeploys old version**

3. **Debug locally** before re-deploying

## Success Criteria

✅ Test suite passes locally  
✅ Telegram messages flow through all 11 layers  
✅ Tools execute correctly via L5  
✅ Security gate blocks anonymous users  
✅ Errors propagate to L10 and notify owner  
✅ No crashes in Railway logs  

## Common Issues

**Issue:** Import errors on Railway  
**Fix:** Check all imports use relative paths or are in PYTHONPATH

**Issue:** Circular import errors  
**Fix:** Layers only import next layer, never backwards

**Issue:** Groq rate limits  
**Fix:** L0 rate limiter already handles this

**Issue:** Mock functions still present  
**Fix:** Search for "mock_" prefix and replace all

## Monitoring

After deployment, watch:
- Railway logs for layer execution traces
- Supabase for error entries
- Telegram for response quality
- Evolution queue for new proposals

## Next Evolution

Once stable:
1. Add L8 coordination layer (multi-agent)
2. Add L6 autonomy loops (background tasks)
3. Add metrics layer (L7 observability)
4. Constitution layer (L10 override)
