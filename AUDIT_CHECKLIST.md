# CORE System Audit Checklist
**Version:** 1.0  
**Last Updated:** 2026-04-03  
**Purpose:** Systematic audit for any session (Claude Desktop, Codex, or manual). Run top-to-bottom. Every CHECK must PASS before marking system healthy.

---

## HOW TO RUN THIS AUDIT

```bash
# Quick automated audit (run this first)
ssh -i ~/.ssh/core-agi-vm.key -o StrictHostKeyChecking=no ubuntu@168.110.217.78 "bash /home/ubuntu/core-agi/run_audit.sh 2>&1"
```

Then verify each section below manually for anything flagged FAIL.

---

## SECTION 1 — INFRASTRUCTURE

### 1.1 All 3 Services Running
```bash
systemctl is-active core-agi core-trading-bot specter-alpha | paste - - -
# EXPECT: active  active  active
```
| Check | Command | Expected |
|---|---|---|
| core-agi | `systemctl is-active core-agi` | `active` |
| core-trading-bot | `systemctl is-active core-trading-bot` | `active` |
| specter-alpha | `systemctl is-active specter-alpha` | `active` |
| nginx | `systemctl is-active nginx` | `active` |

### 1.2 All Ports Listening
```bash
ss -tlnp | grep -E "808[0-9]"
# EXPECT: 8081 (core-agi), 8080 (trading-bot), 8082 (specter-alpha)
```
| Port | Service | Check |
|---|---|---|
| 8081 | core-agi FastAPI | `ss -tlnp \| grep 8081` → python3 process |
| 8080 | trading-bot FastAPI | `ss -tlnp \| grep 8080` → python process |
| 8082 | specter-alpha FastAPI | `ss -tlnp \| grep 8082` → python process |

### 1.3 HTTPS Routing via Nginx
```bash
curl -sk https://core-agi.duckdns.org/ping          # → {"ok":true}
curl -sk https://core-agi.duckdns.org/specter/health # → {"status":"ok","service":"specter-alpha"}
```
| Route | Expected Response |
|---|---|
| `GET /ping` | `{"ok": true, "service": "CORE v6.0"}` |
| `GET /specter/health` | `{"status": "ok", "service": "specter-alpha"}` |
| `POST /mcp/sse` | HTTP 200 (MCP connection) |

### 1.4 SSL Certificate Valid
```bash
curl -v https://core-agi.duckdns.org/ping 2>&1 | grep "SSL certificate\|expire"
# Should show valid cert, not expired
```

---

## SECTION 2 — SUPABASE CONNECTIVITY

### 2.1 Service Role Key Valid (ALL 3 SERVICES)
> ⚠️ **Root cause of 2026-04-03 incident:** JWT keys had stray `"` after `iat` value.  
> Always verify keys decode correctly AND auth succeeds.

```bash
python3 << 'EOF'
import base64, json, urllib.request

envfiles = [
    ('/home/ubuntu/core-agi/.env',      'SUPABASE_SERVICE_KEY',  'core-agi'),
    ('/home/ubuntu/trading-bot/.env',   'SUPABASE_SERVICE_KEY',  'trading-bot'),
    ('/home/ubuntu/specter-alpha/.env', 'SUPABASE_SVC_KEY',      'specter-alpha'),
]
for fpath, key_name, label in envfiles:
    env = {}
    with open(fpath) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip().strip('"')
    url = env.get('SUPABASE_URL', '')
    k = env.get(key_name, '')
    # Decode JWT
    parts = k.split('.')
    payload = parts[1] + '===' if len(parts) > 1 else ''
    try:
        d = json.loads(base64.b64decode(payload))
        role = d.get('role')
    except:
        role = 'CORRUPT'
    # Auth test
    headers = {'apikey': k, 'Authorization': 'Bearer '+k}
    req = urllib.request.Request(url+'/rest/v1/knowledge_base?limit=1', headers=headers)
    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"PASS  {label} {key_name}: role={role} len={len(k)}")
    except Exception as e:
        print(f"FAIL  {label} {key_name}: role={role} — {e}")
EOF
```
| Service | Key Name | Expected |
|---|---|---|
| core-agi | `SUPABASE_SERVICE_KEY` | `role=service_role len=219 PASS` |
| trading-bot | `SUPABASE_SERVICE_KEY` | `role=service_role len=219 PASS` |
| specter-alpha | `SUPABASE_SVC_KEY` | `role=service_role len=219 PASS` |

> 🔧 **If FAIL:** Fetch real key: `curl -s -H "Authorization: Bearer $SUPABASE_PAT" "https://api.supabase.com/v1/projects/$SUPABASE_REF/api-keys"` then replace in all `.env` files.

### 2.2 Core-AGI Supabase Tables Accessible
```bash
cd /home/ubuntu/core-agi && python3 -c "
from core_config import sb_get
for t in ['knowledge_base','task_queue','sessions','behavioral_rules','hot_reflections','evolution_queue','mistakes']:
    try:
        r = sb_get(t, 'id=gt.1&limit=1')
        print(f'PASS  {t}')
    except Exception as e:
        print(f'FAIL  {t}: {e}')
"
```
| Table | Expected |
|---|---|
| `knowledge_base` | PASS |
| `task_queue` | PASS |
| `sessions` | PASS |
| `behavioral_rules` | PASS |
| `hot_reflections` | PASS |
| `evolution_queue` | PASS |
| `mistakes` | PASS |

### 2.3 Trading Bot Supabase Tables Accessible
```bash
cd /home/ubuntu/trading-bot && /home/ubuntu/trading-bot/venv/bin/python -c "
import os
with open('.env') as f:
    for line in f:
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,_,v = line.partition('=')
            os.environ[k.strip()] = v.strip().strip('\"')
from data_collector import sb_get
for t in ['trading_positions','trading_decisions','trading_patterns','trades','trading_pnl_daily','trading_config','market_signals']:
    try:
        sb_get(t, 'limit=1')
        print(f'PASS  {t}')
    except Exception as e:
        print(f'FAIL  {t}: {e}')
"
```
| Table | Expected |
|---|---|
| `trading_positions` | PASS |
| `trading_decisions` | PASS |
| `trading_patterns` | PASS |
| `trades` | PASS |
| `trading_pnl_daily` | PASS |
| `trading_config` | PASS |
| `market_signals` | PASS |

### 2.4 Specter Alpha Supabase Tables Accessible
```bash
cd /home/ubuntu/specter-alpha && /home/ubuntu/specter-alpha/venv/bin/python -c "
import os
with open('.env') as f:
    for line in f:
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,_,v = line.partition('=')
            os.environ[k.strip()] = v.strip().strip('\"')
from signal_listener import sb_get
for t in ['copy_signals','copy_subscribers','copy_payments','copy_orders']:
    try:
        r = sb_get(t, 'limit=1')
        print(f'PASS  {t}')
    except Exception as e:
        print(f'FAIL  {t}: {e}')
"
```
| Table | Expected |
|---|---|
| `copy_signals` | PASS |
| `copy_subscribers` | PASS |
| `copy_payments` | PASS |
| `copy_orders` | PASS |

---

## SECTION 3 — CORE-AGI PIPELINE

### 3.1 Syntax Check — All Core Files
```bash
cd /home/ubuntu/core-agi
for f in core_main.py core_tools.py core_train.py core_orch_agent.py core_config.py \
          core_task_autonomy.py core_evolution_autonomy.py core_research_autonomy.py \
          core_code_autonomy.py core_integration_autonomy.py core_semantic.py \
          core_semantic_projection.py core_repo_map.py; do
  python3 -m py_compile $f 2>&1 && echo "PASS  $f" || echo "FAIL  $f"
done
```

### 3.2 Background Workers Running
```bash
journalctl -u core-agi --no-pager -n 100 | grep -E "\[QUEUE\]|\[COLD\]|\[RESEARCH\]|\[REPO\]|\[GAP\]|\[AUTONOMY\]|\[SEMANTIC\]|\[DIGEST\]" | tail -20
```
| Worker | Log Prefix | Expected |
|---|---|---|
| queue_poller | `[QUEUE] Started` | Seen in startup logs |
| cold_processor_loop | `[COLD]` | Active entries logged |
| background_researcher | `[RESEARCH]` | Running |
| repo_map_loop | `[REPO]` or `[SMAP]` | Running |
| core_gap_audit_loop | `[GAP]` | Running |
| autonomy_loop (task) | `[AUTONOMY]` | Running |
| code_autonomy_loop | `[CODE-AUTO]` | Running |
| integration_autonomy_loop | `[INT-AUTO]` | Running |
| research_autonomy_loop | `[RES-AUTO]` | Running |
| evolution_autonomy_loop | `[EVO-AUTO]` | Running |
| autonomy_digest_loop | `[DIGEST]` | Running |
| semantic_projection_loop | `[SEMANTIC]` | Running |

### 3.3 MCP Endpoint Live
```bash
curl -s --max-time 5 -X POST http://localhost:8081/mcp/sse \
  -H "Authorization: Bearer $MCP_SECRET" 2>/dev/null | head -3
# EXPECT: HTTP 200 (SSE stream opens)
```

### 3.4 Training Pipeline (CORE-AGI)
```bash
# Check hot_reflections being processed
cd /home/ubuntu/core-agi && python3 -c "
from core_config import sb_get
r = sb_get('hot_reflections', 'processed_by_cold=eq.false&id=gt.1&limit=5')
print(f'Unprocessed hot_reflections: {len(r) if r else 0}')
r2 = sb_get('evolution_queue', 'status=eq.pending&id=gt.1&limit=5')
print(f'Pending evolutions: {len(r2) if r2 else 0}')
r3 = sb_get('knowledge_base', 'id=gt.1&limit=1')
print(f'KB accessible: {\"YES\" if r3 is not None else \"NO\"}')
"
```
| Pipeline Step | Expected |
|---|---|
| `hot_reflections` accessible | YES |
| `evolution_queue` accessible | YES |
| `knowledge_base` accessible | YES |

### 3.5 Orchestrator Layers (L0–L11) Importable
```bash
cd /home/ubuntu/core-agi
for i in 0 1 2 3 4 5 6 7 8 9 10 11; do
  python3 -c "import core_orch_layer${i}" 2>&1 && echo "PASS  L$i" || echo "FAIL  L$i"
done
python3 -c "import core_orch_agent; import core_orch_main" && echo "PASS  orch_agent+main" || echo "FAIL"
```

### 3.6 RARL / Autonomy Workers Importable
```bash
cd /home/ubuntu/core-agi
for m in core_task_autonomy core_code_autonomy core_integration_autonomy \
          core_research_autonomy core_evolution_autonomy core_autonomy_digest \
          core_semantic_projection core_repo_map; do
  python3 -c "import $m" 2>&1 && echo "PASS  $m" || echo "FAIL  $m"
done
```

### 3.7 Git State
```bash
cd /home/ubuntu/core-agi
git status --short                    # EXPECT: empty (clean)
git log --oneline -3                  # Show latest commits
git diff origin/main --stat           # EXPECT: nothing
```
> ⚠️ **Note:** `.env` is gitignored (correct). Runtime changes in `.runtime/` may appear dirty — OK.

---

## SECTION 4 — TRADING BOT PIPELINE

### 4.1 Health Endpoint
```bash
curl -s http://localhost:8080/health | python3 -m json.tool
# EXPECT: ok=true, paper_trading=true, all components=true
```
| Component | Expected |
|---|---|
| `ok` | `true` |
| `paper_trading` | `true` (until graduation) |
| `components.supabase` | `true` |
| `components.binance` | `true` |
| `components.telegram` | `true` |
| `components.openrouter` | `true` |

### 4.2 All 8 Background Loops Running
```bash
journalctl -u core-trading-bot --no-pager -n 100 | grep "\[LOOP\]" | tail -20
```
| Loop | Purpose | Interval |
|---|---|---|
| `data_loop` | Collect market snapshots | Every 5 min |
| `decision_loop` | LLM reasoning + trade execution | Every 30 min |
| `momentum_exit_loop` | TP/SL momentum checks | Every 5 min |
| `funding_loop` | Log funding payments | Every 8h |
| `pnl_loop` | Daily P&L summary → Telegram | Daily |
| `graduation_loop` | Check graduation criteria | Weekly |
| `evolution_loop` | Trigger training pipeline | Periodic |
| `polling_loop` | Telegram command polling | Continuous |

### 4.3 Graduation Pipeline
```bash
cd /home/ubuntu/trading-bot
/home/ubuntu/trading-bot/venv/bin/python -c "
from graduation import check_graduation, calculate_performance
result = check_graduation()
print('Graduation ready:', result)
perf = calculate_performance()
print('Trade count:', perf.get('trade_count', 0))
print('Win rate:', perf.get('win_rate', 'N/A'))
print('Sharpe ratio:', perf.get('sharpe_ratio', 'N/A'))
print('Max drawdown:', perf.get('max_drawdown_pct', 'N/A'))
" 2>&1 | grep -v "INFO\|HTTP\|PAPER\|real money\|simulated\|==="
```
**Graduation Criteria (ALL must pass for live):**
| Criterion | Target |
|---|---|
| Win rate | ≥ 80% over last 30 trades |
| Avg confidence | ≥ 0.80 |
| Min trades | ≥ 30 |
| No catastrophic loss | Single trade ≤ 10% capital |
| Market regimes | ≥ 3 different |
| Sharpe ratio | ≥ 1.5 |
| Max drawdown | ≤ 15% |
| Data span | ≥ 20 days |

### 4.4 Training Pipeline Files Present
```bash
ls /home/ubuntu/trading-bot/analysis/run_training_pipeline.py 2>/dev/null && echo "PASS" || echo "FAIL — training pipeline missing"
ls /home/ubuntu/trading-bot/analysis/ | head -10
```

### 4.5 Key Module Imports (via venv)
```bash
cd /home/ubuntu/trading-bot
/home/ubuntu/trading-bot/venv/bin/python -c "
for m in ['brain','executor','graduation','copy_signal_bridge','data_collector',
          'market_classifier','opportunity_engine','portfolio_allocator','bias_engine']:
    try:
        __import__(m)
        print(f'PASS  {m}')
    except Exception as e:
        print(f'FAIL  {m}: {e}')
" 2>&1 | grep -E "PASS|FAIL"
```

### 4.6 Binance Connectivity
```bash
cd /home/ubuntu/trading-bot
/home/ubuntu/trading-bot/venv/bin/python -c "
import os
with open('.env') as f:
    for line in f:
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,_,v = line.partition('=')
            os.environ[k.strip()] = v.strip().strip('\"')
from config import BINANCE_API_KEY, BINANCE_SECRET_KEY
import urllib.request, hmac, hashlib, time
ts = int(time.time() * 1000)
msg = f'timestamp={ts}'.encode()
sig = hmac.new(BINANCE_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()
url = f'https://fapi.binance.com/fapi/v2/balance?timestamp={ts}&signature={sig}'
req = urllib.request.Request(url, headers={'X-MBX-APIKEY': BINANCE_API_KEY})
try:
    r = urllib.request.urlopen(req, timeout=8)
    print('PASS  Binance Futures API connected')
except Exception as e:
    print(f'FAIL  Binance: {e}')
"
```

### 4.7 Git State
```bash
cd /home/ubuntu/trading-bot
git status --short    # EXPECT: empty
git log --oneline -3
```

---

## SECTION 5 — SPECTER ALPHA PIPELINE

### 5.1 Health Endpoint
```bash
curl -s http://localhost:8082/health
# EXPECT: {"status":"ok","service":"specter-alpha"}
```
```bash
curl -sk https://core-agi.duckdns.org/specter/health
# EXPECT: {"status":"ok","service":"specter-alpha"} (via nginx HTTPS)
```

### 5.2 All 4 Background Tasks Running
```bash
journalctl -u specter-alpha --no-pager -n 50 | grep -E "LISTENER|BOT|EXPIRY|started|poll" | tail -10
```
| Task | Function | Expected Log |
|---|---|---|
| `start_bot()` | Telegram subscription bot polling | `[BOT] started` |
| `start_listener()` | Poll `copy_signals` every 5s | `[LISTENER] started polling every 5s` |
| `run_expiry_check()` | Auto-expire lapsed subscribers | `[EXPIRY]` |
| Payment webhook | `/webhook` POST from CryptoBot | Route registered |

### 5.3 Payment → Activation Pipeline
```bash
# Verify webhook route is registered
curl -s http://localhost:8082/openapi.json | python3 -c "
import sys,json
d=json.load(sys.stdin)
paths=list(d.get('paths',{}).keys())
print('Routes:', paths)
# Must include /webhook and /health
for required in ['/webhook','/health']:
    print(f'  {required}: {\"PASS\" if required in paths else \"FAIL\"}')"
```
| Route | Expected |
|---|---|
| `POST /webhook` | PASS (CryptoBot payment receiver) |
| `GET /health` | PASS |

### 5.4 Signal → Dispatch Pipeline
```bash
cd /home/ubuntu/specter-alpha
/home/ubuntu/specter-alpha/venv/bin/python -c "
import os
with open('.env') as f:
    for line in f:
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,_,v = line.partition('=')
            os.environ[k.strip()] = v.strip().strip('\"')
from signal_listener import get_active_subscribers, sb_get
subs = get_active_subscribers()
print(f'Active subscribers: {len(subs) if subs else 0}')
signals = sb_get('copy_signals', 'dispatched=eq.false&limit=5')
print(f'Pending signals: {len(signals) if signals else 0}')
" 2>&1 | grep -v INFO
```

### 5.5 Syntax Check All Specter Files
```bash
cd /home/ubuntu/specter-alpha
for f in copy_service.py signal_listener.py payment_listener.py order_dispatcher.py \
          subscription_bot.py expiry_cron.py key_vault.py models.py config.py; do
  /home/ubuntu/specter-alpha/venv/bin/python -m py_compile $f 2>&1 && echo "PASS  $f" || echo "FAIL  $f"
done
```

### 5.6 Git State
```bash
cd /home/ubuntu/specter-alpha
git status --short    # EXPECT: empty
git log --oneline -3
```

---

## SECTION 6 — INTEGRATION CHECKS

### 6.1 Trading Bot → Specter Signal Bridge
```bash
# Verify copy_signal_bridge can emit (dry run — no actual write)
cd /home/ubuntu/trading-bot
/home/ubuntu/trading-bot/venv/bin/python -c "
import os
with open('.env') as f:
    for line in f:
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,_,v = line.partition('=')
            os.environ[k.strip()] = v.strip().strip('\"')
import copy_signal_bridge
print('PASS  copy_signal_bridge imported — emit_copy_signal() ready')
print('  emit_copy_signal function:', hasattr(copy_signal_bridge, 'emit_copy_signal'))
" 2>&1 | grep -E "PASS|FAIL|emit"
```

### 6.2 Core-AGI → Trading Bot Internal Route
```bash
# Test the internal reflection endpoint
curl -s --max-time 5 -X POST http://localhost:8081/internal/trading/reflect \
  -H "Content-Type: application/json" \
  -d '{"test": true}' 2>/dev/null | head -3
```

### 6.3 Telegram Bot Connectivity
```bash
# Trading bot
cd /home/ubuntu/trading-bot
TGTOKEN=$(grep "TELEGRAM_BOT_TOKEN=" .env | cut -d= -f2- | tr -d '"')
curl -s "https://api.telegram.org/bot${TGTOKEN}/getMe" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('PASS  Trading bot Telegram:', d.get('result',{}).get('username','?')) if d.get('ok') else print('FAIL  Telegram:', d)
"

# Specter bot
SPECTGTOKEN=$(grep "TELEGRAM_BOT_TOKEN=" /home/ubuntu/specter-alpha/.env | cut -d= -f2- | tr -d '"')
curl -s "https://api.telegram.org/bot${SPECTGTOKEN}/getMe" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('PASS  Specter Telegram:', d.get('result',{}).get('username','?')) if d.get('ok') else print('FAIL  Telegram:', d)
"
```

---

## SECTION 7 — POST-AUDIT ACTIONS

### 7.1 If Any Service Was Restarted — Wait for Embedding Backfill
```bash
# core-agi embeds repo_component_edges on startup (~300+ rows) — this blocks /health temporarily
# Wait until embedding storm settles:
journalctl -u core-agi --no-pager -n 5 | grep -v "embedded repo_" | tail -5
# When you see MCP SSE 200 OK and no more [SEMANTIC] lines → fully up
```

### 7.2 Sync to GitHub After Any Code Changes
```bash
git config --global user.email "core@core-agi.duckdns.org"
git config --global user.name "CORE AGI"
for dir in /home/ubuntu/core-agi /home/ubuntu/trading-bot /home/ubuntu/specter-alpha; do
  cd $dir
  git add -A
  git diff --staged --stat
  git commit -m "audit: sync $(date '+%Y-%m-%d %H:%M')" 2>/dev/null || true
  git push origin main 2>&1 | tail -2
done
```
> ⚠️ `.env` files are gitignored — never committed (secrets stay on VM only)

### 7.3 Restart All Services (Clean Restart Order)
```bash
# Restart trading-bot and specter FIRST (don't kill your own shell)
sudo systemctl restart core-trading-bot && echo "trading-bot restarted"
sudo systemctl restart specter-alpha && echo "specter-alpha restarted"
sleep 5

# Restart core-agi LAST (kills the current SSH session — fire and forget)
sudo systemctl restart core-agi &
sleep 15
systemctl is-active core-agi core-trading-bot specter-alpha | paste - - -
```

---

## SECTION 8 — KNOWN ISSUES & RESOLUTIONS

| Issue | Symptom | Resolution |
|---|---|---|
| **Corrupted service_role JWT** | `401 Unauthorized` on Supabase writes | Fetch real key: `curl -s -H "Authorization: Bearer $SUPABASE_PAT" "https://api.supabase.com/v1/projects/$SUPABASE_REF/api-keys"` → replace in all `.env` |
| **`/health` timeout post-restart** | `curl` hangs on 8081/health | Normal — embedding backfill running. Use `/ping` or wait ~5 min. MCP SSE still works. |
| **`git am` stuck** | `git status` shows "in the middle of am session" | `git am --abort` then re-commit |
| **trading-bot uses wrong venv** | `No module named pandas` | Always use `/home/ubuntu/trading-bot/venv/bin/python` not system python3 |
| **Telegram 409 Conflict** | Bot polling returns 409 | Two instances running — `systemctl restart` the service |
| **core-agi MCP tools dead** | All MCP tools timeout | VM crash-loop (import error). SSH in directly, check `journalctl -u core-agi -n 50` |

---

## SECTION 9 — QUICK REFERENCE

### Service Paths
| Service | Repo | Entry Point | Port | Venv |
|---|---|---|---|---|
| CORE-AGI | `/home/ubuntu/core-agi/` | `core_main.py` → uvicorn | 8081 | `/usr/bin/python3` (system) |
| Trading Bot | `/home/ubuntu/trading-bot/` | `trading_bot.py` | 8080 | `/home/ubuntu/trading-bot/venv/bin/python` |
| Specter Alpha | `/home/ubuntu/specter-alpha/` | `copy_service.py` | 8082 | `/home/ubuntu/specter-alpha/venv/bin/python` |

### Supabase Tables Quick Reference
| Service | Tables |
|---|---|
| **CORE-AGI** | `knowledge_base`, `task_queue`, `sessions`, `behavioral_rules`, `hot_reflections`, `evolution_queue`, `mistakes`, `agentic_sessions`, `backlog`, `system_map`, `owner_profile`, `pattern_frequency`, `rarl_epochs`, `cold_reflections` |
| **Trading Bot** | `trading_positions`, `trading_decisions`, `trading_patterns`, `trades`, `trading_pnl_daily`, `trading_config`, `market_signals`, `trading_mistakes`, `copy_signals` |
| **Specter Alpha** | `copy_signals`, `copy_subscribers`, `copy_payments`, `copy_orders` |

### GitHub Repos
```
pockiesaints7/core-agi          → /home/ubuntu/core-agi/
pockiesaints7/core-trading-bot  → /home/ubuntu/trading-bot/
pockiesaints7/specter-alpha     → /home/ubuntu/specter-alpha/
```

### VM Access
```bash
ssh -i ~/.ssh/core-agi-vm.key -o StrictHostKeyChecking=no ubuntu@168.110.217.78
# Or via MCP tool: core-agi:shell(command="...")
```

---

## AUDIT PASS CRITERIA

All sections must show all PASS to declare system healthy:

- [ ] **S1** — All 3 services active, all ports listening, HTTPS routing OK
- [ ] **S2** — All 6 Supabase service_role keys valid, all tables accessible
- [ ] **S3** — Core-AGI: syntax OK, all workers started, MCP live, training pipeline active
- [ ] **S4** — Trading Bot: health OK, all 8 loops running, graduation pipeline callable, Binance connected
- [ ] **S5** — Specter Alpha: health OK, all 4 tasks running, webhook route registered, signal dispatch ready
- [ ] **S6** — Integration: copy_signal_bridge importable, Telegram bots responding
- [ ] **S7** — Git: all repos clean, all pushed to GitHub

**If ANY check fails → fix before marking audit complete.**

