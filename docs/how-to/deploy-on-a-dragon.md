---
audience: operator
type: how-to
prerequisites: ../dev-setup.md, ../ARCHITECTURE.md
last-verified: 2026-05-29
est-time: 15 min
---
# Deploy on a Dragon

Use this when you have a working tree on your workstation and need to push it to
a live [Dragon](../../GLOSSARY.md) running the `dragon_voice` service. This guide
covers the `scp` of code + dashboard + schema, restarting the systemd services,
and the post-deploy checklist that keeps you from chasing ghosts (stale
`__pycache__`, a clobbered `.env`, a missing token, dead ngrok tunnels).

Assumes you already have SSH access to the Dragon and the `tinkerclaw-*` systemd
units installed. If the units do not exist yet, install them first — see
[Developer Environment Setup](../dev-setup.md) for the one-time service install,
or [`systemd/`](https://github.com/lorcan35/TinkerBox/blob/main/systemd) in the
repo root for the canonical unit files.

## Before you start

Confirm the Dragon's current address. The LAN flips between `192.168.1.x` and
`192.168.70.x` and the host is DHCP-managed, so the IP rotates — never assume a
hardcoded value:

```bash
# Resolve by hostname, or scan the subnet for the open ports
ping radxa-dragon-q6a
# → 64 bytes from 192.168.70.242: icmp_seq=1 ttl=64 time=1.2 ms

nmap -p 22,3502,18789 --open 192.168.70.0/24
```

The examples below use `192.168.70.242` (verified 2026-05-04). Substitute the
address you just resolved. The SSH user is `radxa`; sudo on the Dragon is
passwordless after login.

## Steps

### 1. Sync code, dashboard, and schema to the Dragon

`scp` the three things that change on a code push: the `dragon_voice/` package,
`dashboard.py`, and `schema.sql`. Run this from the repo root on your
workstation:

```bash
scp -r dragon_voice/ radxa@192.168.70.242:/home/radxa/
scp dashboard.py radxa@192.168.70.242:/home/radxa/
scp schema.sql radxa@192.168.70.242:/home/radxa/
```

The target user is `radxa` and everything lives under `/home/radxa/` — never
`/home/rock`. The voice service runs as `python3 -m dragon_voice` with
`PYTHONPATH=/home/radxa`, so the package must land at `/home/radxa/dragon_voice`.

### 2. Clear stale `__pycache__` on the Dragon

After any `scp -r`, leftover `.pyc` files can shadow your new source and cause
import errors that look like the deploy never happened. Wipe them before
restarting:

```bash
ssh radxa@192.168.70.242 "find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} +"
```

### 3. Restart the voice service

```bash
ssh radxa@192.168.70.242 "sudo systemctl restart tinkerclaw-voice"
```

`tinkerclaw-voice` (port 3502) is the unit that carries the STT/LLM/TTS pipeline,
the REST API, and the Notes API — it is the one that picks up your code change.
Restart `tinkerclaw-dashboard` (port 3500) too only if you changed `dashboard.py`:

```bash
ssh radxa@192.168.70.242 "sudo systemctl restart tinkerclaw-dashboard"
```

You can chain the cache-clear and the restart into one SSH call:

```bash
ssh radxa@192.168.70.242 \
  "find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} +; sudo systemctl restart tinkerclaw-voice"
```

## Post-deploy checklist

Walk these four after every deploy. They are the difference between "it restarted"
and "it actually works."

- **`__pycache__` cleared.** Covered in step 2. If you skipped it and see
  `ImportError` or behavior that doesn't match your diff, that's the cause — clear
  it and restart again.

- **`.env` survives the push.** The OpenRouter API key and `DRAGON_API_TOKEN`
  live in `/home/radxa/.env`, loaded by systemd via `EnvironmentFile=`. A
  `scp -r dragon_voice/` does **not** touch `/home/radxa/.env`, so secrets
  survive code pushes. Never put real API keys in `config.yaml` in the repo —
  keep them in `.env` so they aren't overwritten and aren't committed. Confirm
  the file is still present and populated:

  ```bash
  ssh radxa@192.168.70.242 "grep -c '=' /home/radxa/.env"
  # → a small positive number (one per VAR=value line)
  ```

- **`tinkerclaw_token` matches the gateway.** If you use the optional TinkerClaw
  sidecar (voice mode 3), the `tinkerclaw_token` in `dragon_voice/config.yaml`
  must match the gateway auth token in `~/.tinkerclaw/tinkerclaw.json`. A
  `config.yaml` overwrite from your deploy can reset it — restore it if so.

- **ngrok tunnels are up.** The `tinkerclaw-ngrok` unit serves three public
  domains that map 1:1 to the local ports:

  | Domain | → Local port | Service |
  |--------|--------------|---------|
  | `tinkerclaw-voice.ngrok.dev` | 3502 | Voice WS + REST API |
  | `tinkerclaw-dashboard.ngrok.dev` | 3500 | Web dashboard |
  | `tinkerclaw-gateway.ngrok.dev` | 18789 | TinkerClaw gateway (localhost-bound) |

  ngrok survives a `tinkerclaw-voice` restart on its own, but confirm it after a
  full reboot — Tab5 falls back to `wss://tinkerclaw-voice.ngrok.dev` when off the
  LAN.

## Verify it worked

Check the service is active and not crash-looping:

```bash
ssh radxa@192.168.70.242 "sudo systemctl status tinkerclaw-voice --no-pager"
# → Active: active (running) since ...
```

Hit the health endpoint over the LAN:

```bash
curl -s http://192.168.70.242:3502/health
# → {"status": "ok", ...}
```

Tail the logs for a clean startup — no tracebacks, backends initialized:

```bash
ssh radxa@192.168.70.242 "journalctl -u tinkerclaw-voice -n 40 --no-pager"
```

Confirm the ngrok path resolves from outside the LAN:

```bash
curl -s https://tinkerclaw-voice.ngrok.dev/health
# → {"status": "ok", ...}
```

For a deeper smoke test against the running server, run the live e2e suite from
your workstation (it expects the Dragon reachable on `:3502`):

```bash
python3 tests/test_api_e2e.py
```

## Service map reference

The units you interact with on a deploy:

| Service | Port | systemd unit | Restart when… |
|---------|------|--------------|---------------|
| Voice + API | 3502 | `tinkerclaw-voice` | you changed `dragon_voice/` or `schema.sql` |
| Dashboard | 3500 | `tinkerclaw-dashboard` | you changed `dashboard.py` |
| mDNS | — | `tinkerclaw-mdns` | rarely — advertises `_tinkerclaw._tcp` |
| ngrok | 443 (ext) | `tinkerclaw-ngrok` | after a full reboot, to confirm tunnels |
| TinkerClaw gateway | 18789 | `tinkerclaw-gateway` | only if running voice mode 3 |

## Troubleshooting

- **`ImportError` or stale behavior after deploy** → leftover `.pyc` files.
  Re-run the `__pycache__` clear from step 2, then restart.

- **LLM calls fail with auth errors** → `/home/radxa/.env` is missing or empty.
  The `OPENROUTER_API_KEY` and `DRAGON_API_TOKEN` are loaded from there via the
  unit's `EnvironmentFile=`. Restore the file (it is not pushed by `scp`) and
  restart `tinkerclaw-voice`.

- **`scp` lands code under the wrong path / `python3 -m dragon_voice` can't find
  the package** → the user is `radxa` and everything lives under `/home/radxa/`,
  with `PYTHONPATH=/home/radxa`. Verify `/home/radxa/dragon_voice/__main__.py`
  exists on the Dragon.

- **Connection refused / wrong IP** → the Dragon is DHCP and the LAN rotates
  between `192.168.1.x` and `192.168.70.x`. Re-resolve with
  `ping radxa-dragon-q6a` or `nmap -p 22,3502,18789 --open <subnet>/24` and reuse
  that address for every command.

- **`Address already in use` on port 3502** → a stale process is holding the
  port. Find and kill it, or let systemd own it:
  `ssh radxa@192.168.70.242 "sudo lsof -i :3502"` then
  `sudo systemctl restart tinkerclaw-voice`.

- **Voice mode 3 (TinkerClaw) returns errors after deploy** → the
  `tinkerclaw_token` in `config.yaml` no longer matches
  `~/.tinkerclaw/tinkerclaw.json`. Restore the matching token and restart.

- **Tab5 can't reach the Dragon off-LAN** → an ngrok tunnel is down. Check
  `sudo systemctl status tinkerclaw-ngrok` and confirm the three domains in the
  post-deploy checklist resolve.

## Related

- [Developer Environment Setup](../dev-setup.md) — first-time clone, `.env`,
  service install, and the workstation deploy workflow.
- [Architecture](../ARCHITECTURE.md) — the map of what runs where on the Dragon.
- [WebSocket protocol](../protocol.md) — the Tab5 ↔ Dragon contract your deploy
  has to keep honoring.
- [NPU setup](../npu-setup.md) — the Qualcomm Genie/HTP LLM path on the Dragon.
- [`CLAUDE.md`](../../CLAUDE.md) — the full operator runbook (debug, monitor,
  restart, service internals).
