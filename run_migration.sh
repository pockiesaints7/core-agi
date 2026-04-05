#!/bin/bash
# run_migration.sh — One-shot migration runner
# Bootstraps new Supabase schema + imports data + restarts services
# Runs with clean env (no old process env vars)

set -e
cd /home/ubuntu/core-agi

echo '[MIGRATION] Starting Supabase migration to new project...'

# Load fresh env from file
export $(grep -v '^#' /home/ubuntu/core-agi/.env | grep -v '^$' | xargs)

echo "[MIGRATION] Target: $SUPABASE_URL"
echo "[MIGRATION] Ref: $SUPABASE_REF"

# Step 1: Bootstrap schema
echo '[MIGRATION] Step 1: Bootstrap schema...'
python3 core_supabase_bootstrap.py 2>&1 | tail -5

# Step 2: Import data
echo '[MIGRATION] Step 2: Import data...'
python3 /home/ubuntu/import_supabase.py 2>&1

# Step 3: Bootstrap trading bot
echo '[MIGRATION] Step 3: Bootstrap trading bot schema...'
export $(grep -v '^#' /home/ubuntu/trading-bot/.env | grep -v '^$' | xargs) 2>/dev/null || true
cd /home/ubuntu/trading-bot && python3 bootstrap_supabase.py 2>&1 | tail -5
cd /home/ubuntu/core-agi

# Step 4: Start core-agi
echo '[MIGRATION] Step 4: Starting core-agi...'
systemctl start core-agi
sleep 8
systemctl is-active core-agi && echo '[MIGRATION] core-agi UP' || echo '[MIGRATION] core-agi FAILED'

echo '[MIGRATION] Done. Check: journalctl -u core-agi -n 30'
