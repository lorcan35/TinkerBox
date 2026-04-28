# Developer Environment Setup

> Get from "I cloned the repo" to "I can run + iterate + flash"
> for both Dragon (Python) and Tab5 (ESP-IDF firmware).
> Estimated time: 30-45 minutes the first time.

This is the cross-repo setup doc.  Tab5 firmware setup lives in
the TinkerTab side; Dragon Python setup lives here.  You probably
need both unless you only care about one half.

---

## Prerequisites

| Requirement | Why |
|-------------|-----|
| **Linux or macOS workstation** | ESP-IDF is best on Linux; Dragon deploys via SSH from a Unix env |
| **Git + GitHub CLI (`gh`)** | Branching, PRs |
| **Python 3.12+** | Dragon Python service |
| **Dragon Q6A SBC** (optional but recommended for full flow) | The actual server |
| **M5Stack Tab5 hardware** (optional) | Otherwise the firmware doesn't have anywhere to flash |
| **OpenRouter API key** (optional, free to start) | Cloud-mode LLM access |

If you don't have Tab5/Dragon hardware, you can still iterate on Dragon Python code and run the unit-test suite (556 tests, 11s on a laptop).

---

## Part 1 — Dragon (this repo, TinkerBox)

### 1.1 Clone + venv

```bash
git clone https://github.com/lorcan35/TinkerBox.git
cd TinkerBox

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note:** On Dragon itself, `pip install --break-system-packages` is required (PEP 668).  On a workstation venv, the above is fine.

### 1.2 Set up `.env`

Create `~/.env` (or copy to Dragon as `/home/radxa/.env`):

```bash
# Required for Cloud / Hybrid voice modes
OPENROUTER_API_KEY=sk-or-v1-...

# Required for Dragon REST API auth (any random 32-char hex)
DRAGON_API_TOKEN=$(openssl rand -hex 32)

# Optional: TinkerClaw gateway (only if you set up the sidecar)
TINKERCLAW_TOKEN=$(openssl rand -hex 24)
```

Get an OpenRouter key at <https://openrouter.ai/keys> — they have a free tier good for development.

### 1.3 Test locally

```bash
# Run the unit test suite (no Dragon hardware needed)
python3 -m pytest tests/ --ignore=tests/audit -q

# Should finish in ~11 seconds with 556 tests passing.
```

If tests pass, your Python environment is good.

### 1.4 Run the voice server locally

```bash
# Start the Dragon-side voice service on port 3502
python3 -m dragon_voice
```

You should see logs about Moonshine STT loading, Piper TTS loading, the LLM backend (defaults to ollama / ministral-3:3b).  If you don't have ollama running locally, it'll log warnings but still start.

Visit `http://localhost:3502/health` — should return `{"status":"ok",...}`.

To run with a specific config:

```bash
DRAGON_API_TOKEN=ci-bearer-token python3 -m dragon_voice --log-level DEBUG
```

### 1.5 SSH access to Dragon (optional)

If you have a Dragon Q6A and want to deploy to it:

```bash
# Add your SSH key to Dragon's authorized_keys
ssh-copy-id radxa@192.168.1.91

# Test passwordless login
ssh radxa@192.168.1.91 'uname -a'

# Deploy your local working tree
scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/

# Restart the service to pick up changes
ssh radxa@192.168.1.91 'sudo systemctl restart tinkerclaw-voice'

# Watch logs
ssh radxa@192.168.1.91 'sudo journalctl -u tinkerclaw-voice -f'
```

**Important:** After every `scp`, clear `__pycache__` on Dragon to avoid stale `.pyc` import errors:

```bash
ssh radxa@192.168.1.91 'find /home/radxa/dragon_voice -name __pycache__ -exec rm -rf {} +'
```

### 1.6 ngrok setup (optional but recommended)

If you want Tab5 to reach Dragon when you're not on the same WiFi:

```bash
# On Dragon
ngrok config add-authtoken <your-ngrok-token>

# Start the systemd unit (already configured in the repo)
sudo systemctl enable --now tinkerclaw-ngrok
```

Three tunnels go up:
- `tinkerclaw-voice.ngrok.dev` → 3502 (voice WS)
- `tinkerclaw-dashboard.ngrok.dev` → 3500 (dashboard)
- `tinkerclaw-gateway.ngrok.dev` → 18789 (TinkerClaw — optional)

Tab5 firmware tries the LAN first then falls back to ngrok automatically (`conn_m=0` default).

### 1.7 Ollama setup (for Local mode)

```bash
# On Dragon (or local for testing)
curl -fsSL https://ollama.com/install.sh | sh

# Pull the default model
ollama pull ministral-3:3b
ollama pull nomic-embed-text  # for embeddings (memory + RAG)

# Verify
ollama list
```

For the multi-model router, see [`router-cookbook.md`](router-cookbook.md) for which models to pull.

---

## Part 2 — Tab5 firmware (TinkerTab repo)

### 2.1 Clone TinkerTab

```bash
git clone https://github.com/lorcan35/TinkerTab.git ~/projects/TinkerTab
cd ~/projects/TinkerTab
```

### 2.2 Install ESP-IDF v5.5.2

The firmware is **pinned to ESP-IDF v5.5.2** (per `dependencies.lock`).  Don't use a different version — the manifest will fail to resolve components.

```bash
# Install via the official installer
mkdir -p ~/esp
cd ~/esp
git clone -b v5.5.2 --recursive https://github.com/espressif/esp-idf.git

cd ~/esp/esp-idf
./install.sh esp32p4

# Source the env script (do this in every shell that builds firmware)
source ~/esp/esp-idf/export.sh
```

Confirm with:

```bash
idf.py --version
# Should print: ESP-IDF v5.5.2
```

### 2.3 Set the target

```bash
cd ~/projects/TinkerTab
idf.py set-target esp32p4
```

This generates `sdkconfig` from `sdkconfig.defaults`.

### 2.4 Configure WiFi + Dragon connection

```bash
idf.py menuconfig
```

Navigate to:
- **TinkerClaw → WiFi credentials** — set SSID + password
- **TinkerClaw → Dragon host/port** — defaults to 192.168.1.91:3502
- **TinkerClaw → Dragon API token** — paste the value of `DRAGON_API_TOKEN` from Dragon's `.env`

Save and exit.

For non-interactive setup, edit `sdkconfig.local`:

```text
CONFIG_TAB5_WIFI_SSID="MyHomeWifi"
CONFIG_TAB5_WIFI_PASS="hunter2"
CONFIG_TAB5_DRAGON_HOST="192.168.1.91"
CONFIG_TAB5_DRAGON_PORT=3502
CONFIG_TAB5_DRAGON_TOKEN="your-token-here"
```

### 2.5 Build

```bash
idf.py build
```

First build takes ~5-10 minutes.  Subsequent incrementals are 30-90 seconds.

If you change `sdkconfig.defaults` or pull new components:

```bash
idf.py fullclean build
```

### 2.6 Flash + monitor

Plug Tab5 into a USB-C port on your workstation.  It enumerates as `/dev/ttyACM0` on Linux.

```bash
idf.py -p /dev/ttyACM0 flash

# If the board enters ROM-download mode after flashing (common on
# ESP32-P4), trigger a watchdog reset:
python -m esptool --chip esp32p4 -p /dev/ttyACM0 \
    --before no_reset --after watchdog_reset read_mac
```

Monitor serial output:

```bash
python3 -c "
import serial, time
s = serial.Serial('/dev/ttyACM0', 115200, timeout=5)
time.sleep(0.3)
s.write(b'\r')
while True:
    if s.in_waiting:
        print(s.read(s.in_waiting).decode('utf-8', errors='replace'), end='', flush=True)
"
```

Or use `idf.py monitor` (Ctrl+] to exit).

### 2.7 USB permissions on Linux

If you get `Permission denied: '/dev/ttyACM0'`:

```bash
sudo usermod -a -G dialout $USER
# Log out and back in for the group change to take effect.
```

### 2.8 First-boot Tab5 verification

After flash, on a phone or laptop on the same WiFi:

```bash
curl -s http://<tab5-ip>:8080/info | python3 -m json.tool
```

Should show `voice_connected: true` if Dragon is reachable.

The auth token for the debug server is on the serial log (look for `auth token:`) — masked but the first/last 4 chars confirm which token is active.  Full token recoverable via `esptool read_flash 0x9000 0x6000` if needed.

---

## Part 3 — Iteration loops

### Dragon-only change

```bash
# 1. Edit dragon_voice/...
# 2. Run unit tests
pytest -q tests/test_<your_area>.py

# 3. Lint
ruff check --select F821,F722,F811,F823,B006,B904,E722,B007,RUF006 dragon_voice/

# 4. Deploy to Dragon
scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
ssh radxa@192.168.1.91 'find /home/radxa/dragon_voice -name __pycache__ -exec rm -rf {} +; sudo systemctl restart tinkerclaw-voice'

# 5. Watch logs
ssh radxa@192.168.1.91 'sudo journalctl -u tinkerclaw-voice -f --since "30 seconds ago"'
```

### Tab5-only change

```bash
. ~/esp/esp-idf/export.sh
cd ~/projects/TinkerTab
idf.py build && idf.py -p /dev/ttyACM0 flash
python -m esptool --chip esp32p4 -p /dev/ttyACM0 --before no_reset --after watchdog_reset read_mac
```

### Cross-stack change (touches both)

1. Make Dragon change first (the protocol-extending side).
2. Test Dragon change with the existing Tab5 firmware (Tab5 ignores unknown fields).
3. Make Tab5 change.
4. Update `docs/protocol.md` *in the same PR* as the protocol-changing side.

### E2E harness for full-system testing

The Python harness in [`tests/e2e/`](https://github.com/lorcan35/TinkerTab/blob/main/tests/e2e/) (TinkerTab repo) drives Tab5 through long user-story scenarios via the debug HTTP API.

```bash
cd ~/projects/TinkerTab
export TAB5_URL=http://<tab5-ip>:8080
export TAB5_TOKEN=<auth_tok-from-NVS>

# Three scenarios
python3 tests/e2e/runner.py story_smoke    # ~2 min
python3 tests/e2e/runner.py story_full     # ~2 min
python3 tests/e2e/runner.py story_stress   # ~10 min

# All three with a clean reboot first
python3 tests/e2e/runner.py all --reboot
```

Reports + screenshots land in `tests/e2e/runs/<scenario>-<timestamp>/`.

---

## Part 4 — Common pitfalls

### "Cannot find ESP-IDF environment"

You forgot to `source ~/esp/esp-idf/export.sh`.  Add it to your shell rc to skip the manual step:

```bash
# in ~/.bashrc or ~/.zshrc
alias idfenv='. ~/esp/esp-idf/export.sh'
```

### "Connection refused" on `localhost:3502`

You haven't started `dragon_voice`.  See section 1.4.

### Tests pass locally but CI fails

Likely the test you added uses a hardcoded path or token instead of env vars.  CI uses `DRAGON_API_TOKEN=ci-bearer-token`.

### `__pycache__` issues after deploy

```bash
ssh radxa@192.168.1.91 'find /home/radxa/dragon_voice -name __pycache__ -exec rm -rf {} +'
```

This is annoying enough that we recommend baking it into your deploy script.

### `idf.py build` complains about target

```bash
idf.py set-target esp32p4
```

After every `git checkout` of a different branch.

### Tab5 doesn't connect to Dragon

1. Check `dragon_host` in NVS via `GET /settings`:
   ```bash
   curl -s -H "Authorization: Bearer $TAB5_TOKEN" http://<tab5-ip>:8080/settings | python3 -m json.tool | grep dragon
   ```
2. If wrong, fix it:
   ```bash
   curl -X POST -H "Authorization: Bearer $TAB5_TOKEN" http://<tab5-ip>:8080/settings -d '{"dragon_host":"192.168.1.91"}'
   ```
3. Reboot Tab5:
   ```bash
   curl -X POST -H "Authorization: Bearer $TAB5_TOKEN" http://<tab5-ip>:8080/reboot
   ```

### Stale firmware on Tab5

```bash
# Force-reflash after a partition change
idf.py -p /dev/ttyACM0 erase-flash flash
```

---

## Part 5 — Where to go next

You're set up.  Pick a thread:

- **Add a new agentic tool:** [`adding-a-tool.md`](adding-a-tool.md)
- **Add a new LLM model to the fleet:** [`router-cookbook.md`](router-cookbook.md)
- **Author a skill that emits widgets:** [`SKILL_AUTHORING.md`](SKILL_AUTHORING.md)
- **Add a channel adapter:** [`telegram-bot.md`](telegram-bot.md) is the reference example
- **Trace one request through the whole system:** [`flows/voice-turn.md`](flows/voice-turn.md)
- **Read the war stories:** [`../LEARNINGS.md`](../LEARNINGS.md)

Welcome aboard. 🐉
