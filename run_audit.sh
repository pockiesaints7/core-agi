#!/bin/bash
# ============================================================
# CORE SYSTEM AUTOMATED AUDIT SCRIPT
# Run: bash /home/ubuntu/core-agi/run_audit.sh
# ============================================================
PASS=0; FAIL=0
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'; BOLD='\033[1m'

check() {
    local label="$1"; local cmd="$2"; local expect="$3"
    result=$(eval "$cmd" 2>/dev/null)
    if echo "$result" | grep -q "$expect"; then
        echo -e "  ${GREEN}PASS${NC}  $label"
        ((PASS++))
    else
        echo -e "  ${RED}FAIL${NC}  $label  [got: $(echo $result | head -c 80)]"
        ((FAIL++))
    fi
}

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         CORE SYSTEM AUDIT — $(date '+%Y-%m-%d %H:%M WIB')        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S1] INFRASTRUCTURE${NC}"
check "core-agi service"      "systemctl is-active core-agi"      "active"
check "core-trading-bot"      "systemctl is-active core-trading-bot" "active"
check "specter-alpha"         "systemctl is-active specter-alpha"  "active"
check "nginx"                 "systemctl is-active nginx"          "active"
check "port 8081 (core-agi)"  "ss -tlnp | grep 8081"              "8081"
check "port 8080 (trading)"   "ss -tlnp | grep 8080"              "8080"
check "port 8082 (specter)"   "ss -tlnp | grep 8082"              "8082"
check "HTTPS /specter/health" "curl -sk --max-time 6 https://core-agi.duckdns.org/specter/health" "specter-alpha"

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S2] SUPABASE KEYS & TABLES${NC}"

python3 << 'PYEOF'
import base64, json, urllib.request, sys

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
pass_count=0; fail_count=0

def check_key(fpath, key_name, label):
    global pass_count, fail_count
    env = {}
    with open(fpath) as f:
        for line in f:
            line=line.strip()
            if '=' in line and not line.startswith('#'):
                k,_,v = line.partition('=')
                env[k.strip()] = v.strip().strip('"')
    url = env.get('SUPABASE_URL','')
    k = env.get(key_name,'')
    if not k:
        print(f"  {RED}FAIL{NC}  {label} — key EMPTY")
        fail_count += 1; return
    parts = k.split('.')
    try:
        payload = parts[1] + '==='
        d = json.loads(base64.b64decode(payload))
        role = d.get('role','?')
    except:
        print(f"  {RED}FAIL{NC}  {label} — JWT CORRUPT")
        fail_count += 1; return
    headers = {'apikey': k, 'Authorization': 'Bearer '+k}
    req = urllib.request.Request(url+'/rest/v1/knowledge_base?limit=1', headers=headers)
    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"  {GREEN}PASS{NC}  {label} (role={role} len={len(k)})")
        pass_count += 1
    except Exception as e:
        print(f"  {RED}FAIL{NC}  {label} — AUTH FAIL: {e}")
        fail_count += 1

check_key('/home/ubuntu/core-agi/.env',      'SUPABASE_SERVICE_KEY', 'core-agi service_role key')
check_key('/home/ubuntu/trading-bot/.env',   'SUPABASE_SERVICE_KEY', 'trading-bot service_role key')
check_key('/home/ubuntu/specter-alpha/.env', 'SUPABASE_SVC_KEY',     'specter service_role key')

# Table checks
import os, sys
sys.path.insert(0, '/home/ubuntu/core-agi')
os.environ.setdefault('DUMMY','1')

try:
    env={}
    with open('/home/ubuntu/core-agi/.env') as f:
        for line in f:
            line=line.strip()
            if '=' in line and not line.startswith('#'):
                k,_,v = line.partition('=')
                env[k.strip()] = v.strip().strip('"')
    for k,v in env.items():
        os.environ[k] = v
    from core_config import sb_get
    for t in ['knowledge_base','task_queue','sessions','behavioral_rules','hot_reflections','evolution_queue']:
        try:
            sb_get(t, 'id=gt.1&limit=1')
            print(f"  {GREEN}PASS{NC}  core-agi table: {t}")
            pass_count += 1
        except Exception as e:
            print(f"  {RED}FAIL{NC}  core-agi table: {t} — {e}")
            fail_count += 1
except Exception as e:
    print(f"  {RED}FAIL{NC}  core-agi table import: {e}")
    fail_count += 1

# Write results to a temp file for main script to read
with open('/tmp/audit_py_result.txt','w') as f:
    f.write(f"{pass_count} {fail_count}")
PYEOF

result=$(cat /tmp/audit_py_result.txt 2>/dev/null || echo "0 0")
PASS=$((PASS + $(echo $result | awk '{print $1}')))
FAIL=$((FAIL + $(echo $result | awk '{print $2}')))

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S3] CORE-AGI PIPELINE${NC}"
cd /home/ubuntu/core-agi
for f in core_main.py core_tools.py core_train.py core_orch_agent.py core_config.py; do
    check "syntax: $f" "python3 -m py_compile $f && echo OK" "OK"
done
check "MCP SSE port open"   "ss -tlnp | grep 8081"                  "8081"
check "hot_reflections col" "grep -r 'processed_by_cold' core_train.py" "processed_by_cold"
check "orch layers L0-L11"  "python3 -c 'import core_orch_layer0,core_orch_layer11' && echo OK" "OK"
check "autonomy workers"    "python3 -c 'import core_task_autonomy,core_evolution_autonomy,core_research_autonomy' && echo OK" "OK"

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S4] TRADING BOT PIPELINE${NC}"
VENV=/home/ubuntu/trading-bot/venv/bin/python
check "health endpoint"     "curl -s --max-time 5 http://localhost:8080/health" '"ok":true'
check "supabase component"  "curl -s --max-time 5 http://localhost:8080/health" '"supabase":true'
check "binance component"   "curl -s --max-time 5 http://localhost:8080/health" '"binance":true'
check "paper_trading mode"  "curl -s --max-time 5 http://localhost:8080/health" '"paper_trading":true'
check "8 loops defined"     "grep -c 'threading.Thread' /home/ubuntu/trading-bot/trading_bot.py" "[89]"
check "graduation callable" "cd /home/ubuntu/trading-bot && $VENV -c 'from graduation import check_graduation; check_graduation()' && echo OK" "OK"
check "copy_signal_bridge"  "cd /home/ubuntu/trading-bot && $VENV -c 'import copy_signal_bridge; print(hasattr(copy_signal_bridge,\"emit_copy_signal\"))'" "True"
check "training pipeline"   "ls /home/ubuntu/trading-bot/analysis/run_training_pipeline.py" "run_training_pipeline"

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S5] SPECTER ALPHA PIPELINE${NC}"
SVENV=/home/ubuntu/specter-alpha/venv/bin/python
check "health endpoint"     "curl -s --max-time 5 http://localhost:8082/health" 'specter-alpha'
check "HTTPS /specter/"     "curl -sk --max-time 6 https://core-agi.duckdns.org/specter/health" 'specter-alpha'
check "signal_listener"     "cd /home/ubuntu/specter-alpha && $SVENV -c 'from signal_listener import start_listener; print(\"OK\")'" "OK"
check "payment webhook"     "curl -s --max-time 5 http://localhost:8082/openapi.json" '/webhook'
check "order_dispatcher"    "cd /home/ubuntu/specter-alpha && $SVENV -c 'from order_dispatcher import execute_for_subscriber; print(\"OK\")'" "OK"
check "expiry_cron"         "cd /home/ubuntu/specter-alpha && $SVENV -c 'from expiry_cron import run_expiry_check; print(\"OK\")'" "OK"
for f in copy_service.py signal_listener.py payment_listener.py order_dispatcher.py models.py; do
    check "syntax: $f" "cd /home/ubuntu/specter-alpha && $SVENV -m py_compile $f && echo OK" "OK"
done

# ─────────────────────────────────────────────
echo -e "\n${BOLD}[S6] INTEGRATION${NC}"
check "git core-agi clean"     "cd /home/ubuntu/core-agi && git status --short | wc -l" "^0$"
check "git trading-bot clean"  "cd /home/ubuntu/trading-bot && git status --short | wc -l" "^0$"
check "git specter clean"      "cd /home/ubuntu/specter-alpha && git status --short | wc -l" "^0$"

# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
TOTAL=$((PASS+FAIL))
if [ $FAIL -eq 0 ]; then
    echo -e "${BOLD}║  ${GREEN}ALL PASS: $PASS/$TOTAL checks${NC}${BOLD} — SYSTEM HEALTHY ✓      ║${NC}"
else
    echo -e "${BOLD}║  ${RED}FAILED: $FAIL/$TOTAL checks${NC}${BOLD} — REVIEW REQUIRED ✗      ║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

[ $FAIL -eq 0 ] && exit 0 || exit 1
