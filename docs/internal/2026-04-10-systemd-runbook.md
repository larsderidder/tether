# Tether systemd runbook

Tether now runs as a user service from the repo checkout.

## Service

- Unit: `~/.config/systemd/user/tether.service`
- Start script: `scripts/run-systemd.sh`
- Working tree: `/home/lars/xithing/tether`
- Python: `~/.virtualenvs/tether/bin/python`

The start script rebuilds the UI and bundled sidecars before launching `python -m tether.main` from `agent/`.

## Commands

```bash
systemctl --user status tether.service
systemctl --user restart tether.service
systemctl --user stop tether.service
systemctl --user start tether.service
journalctl --user -u tether.service -n 200 --no-pager
journalctl --user -u tether.service -f
curl http://127.0.0.1:8787/api/health
```

## Notes

- The service is enabled and will start automatically on login and because linger is enabled, it should keep running without an active terminal session.
- Config still comes from the repo `.env` and any fallback settings in `~/.config/tether/config.env`.
- Current startup logs show a Telegram bridge token problem and an Alembic revision warning. The web app still starts successfully, but those two items should be cleaned up before relying on Telegram.
