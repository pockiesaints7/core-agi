# Systemd Services

This directory contains the reproducible service templates and installer for the
CORE VM.

Install:

```bash
sudo bash deploy/systemd/install_all_services.sh
```

This installs and enables only:

- `core-agi.service`
- `core-trading-bot.service`
- `specter-alpha.service`

`vm_autosync.service` is intentionally not installed by default. Re-enable it
manually only after all three repos are clean, committed, and pushed.

The templates assume the repos live under `/home/ubuntu/` on the target VM:

- `/home/ubuntu/core-agi`
- `/home/ubuntu/trading-bot`
- `/home/ubuntu/specter-alpha`

If you use a different checkout path, update the unit files before installing
or symlink the repos into those locations.
