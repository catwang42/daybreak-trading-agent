# Alternative — Compute Engine + cron
When to prefer: you want an always-on box for ad-hoc interactive runs, persistent local files without GCS, or to later host anything long-running. Cost: e2-micro is free-tier eligible in some US regions (otherwise a few $/mo); e2-small if memory-tight.
Setup sketch: create VM (Debian) → clone repo → venv + requirements → config/.env (or fetch from Secret Manager at boot) → crontab: `0 8 * * 1-5 TZ=America/New_York cd ~/trading-agent && .venv/bin/python -m tradingagent --stage all >> ~/cron.log 2>&1` → same Telegram delivery.
Trade-offs vs Cloud Run Jobs: you patch/maintain the VM, no scale-to-zero, but simpler mental model and free-tier possible.
