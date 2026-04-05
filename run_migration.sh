#!/bin/bash
# run_migration.sh — One-shot migration: bootstrap schema + import data + restart
# Triggered by auto-deploy pull after git push
set -e

LOG=/tmp/migration_$(date +%Y%m%d_%H%M%S).log
exec > >(tee -a $LOG) 2>&1

echo '[MIGRATION] ============================================'
echo '[MIGRATION] Starting Supabase migration to new project'
echo '[MIGRATION] ============================================'

# Load fresh env from file — NOT from process environment
set -a
source /home/ubuntu/core-agi/.env
set +a

echo "[MIGRATION] Target: $SUPABASE_URL"
echo "[MIGRATION] Ref:    $SUPABASE_REF"

# Validate we are on new project
if [[ "$SUPABASE_REF" != "cdnibaebtfmkzshuzlbk" ]]; then
  echo "[MIGRATION] ERROR: SUPABASE_REF is $SUPABASE_REF, expected cdnibaebtfmkzshuzlbk"
  exit 1
fi

# Step 1: Bootstrap schema on new project
echo
echo '[MIGRATION] Step 1: Bootstrap schema...'
cd /home/ubuntu/core-agi
python3 core_supabase_bootstrap.py 2>&1 | tail -10

# Step 2: Import data
echo
echo '[MIGRATION] Step 2: Import data...'
python3 /home/ubuntu/import_supabase.py 2>&1

# Step 3: Bootstrap trading bot schema
echo
echo '[MIGRATION] Step 3: Trading bot schema...'
set -a
source /home/ubuntu/trading-bot/.env
set +a
cd /home/ubuntu/trading-bot
python3 bootstrap_supabase.py 2>&1 | tail -5
cd /home/ubuntu/core-agi

# Step 4: Start core-agi
echo
echo '[MIGRATION] Step 4: Starting core-agi...'
systemctl start core-agi
sleep 10
STATUS=$(systemctl is-active core-agi)
echo "[MIGRATION] core-agi status: $STATUS"

echo
echo "[MIGRATION] Log saved to: $LOG"
echo '[MIGRATION] Done. Run: journalctl -u core-agi -n 50'
