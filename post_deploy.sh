#!/bin/bash
# post_deploy.sh — Runs after git pull by auto-deploy service
# Place this in /home/ubuntu/core-agi/ and reference from auto-deploy

# If migration flag exists, run migration first
if [ -f /tmp/run_migration_flag ]; then
  rm -f /tmp/run_migration_flag
  bash /home/ubuntu/core-agi/run_migration.sh
else
  # Normal deploy: just restart
  systemctl restart core-agi
fi
