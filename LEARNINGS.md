# LEARNINGS.md -- TinkerClaw Institutional Knowledge

This is a living document of hard-won lessons from the TinkerClaw project.
It covers the Dragon-side server (ARM64, Radxa Zero 3W) as well as
cross-cutting concerns with TinkerTab (ESP32-P4 firmware).

**How to add entries:** Append under the appropriate category using the template
below. If no category fits, create a new `##` section. Number entries
sequentially across the whole file (don't restart per section).

```
### [Short Title]
- **Date:** YYYY-MM-DD
- **Symptom:** What was observed
- **Root Cause:** Why it happened
- **Fix:** What was done
- **Prevention:** How to avoid it in the future
```

---

## Dragon/ARM64 Quirks

### 1. Piper TTS model permissions
- **Date:** 2026-03-29
- **Symptom:** PermissionError when the voice service tried to load Piper TTS model files.
- **Root Cause:** Models were cached under `/home/rock/.cache/` but the voice service systemd unit had `User=radxa` after a user migration. The radxa user had no read access to rock's cache directory.
- **Fix:** Copied model caches to the correct user's home (`/home/radxa/.cache/`) and ensured the `User=` directive in the systemd unit matched the owner of the files.
- **Prevention:** Whenever the system user changes, audit every systemd service's `User=` and `WorkingDirectory=` against actual file ownership. Run `ls -la` on cache/model dirs before declaring a service ready.

### 2. rsync unavailable on Radxa
- **Date:** 2026-03-29
- **Symptom:** `rsync: command not found` when trying to sync files to Dragon.
- **Root Cause:** The Radxa Zero 3W minimal image does not ship with rsync.
- **Fix:** Used `cp -r` for local copies and `scp` for remote transfers instead.
- **Prevention:** Do not assume rsync exists on embedded/minimal ARM images. Use `cp -r` or `scp` as the default file-transfer method for Dragon.

### 3. Moonshine STT on ARM64
- **Date:** 2026-03-28
- **Symptom:** whisper.cpp inference was far too slow on ARM64 for anything resembling real-time speech-to-text.
- **Root Cause:** whisper.cpp's compute requirements exceed what the Radxa Zero 3W's Cortex-A55 cores can deliver in a reasonable latency window.
- **Fix:** Replaced whisper.cpp with Moonshine V2 running on ONNX Runtime. Faster inference, better accuracy for the target use case.
- **Prevention:** Always benchmark STT candidates on the actual target hardware before committing to an engine. ARM64 is not x86 -- model speed does not transfer.

### 4. Ollama inference latency
- **Date:** 2026-03-29
- **Symptom:** Full STT to LLM to TTS cycle takes approximately 20 seconds with gemma3:4b on ARM64.
- **Root Cause:** ARM64 cores are slow for LLM inference; gemma3:4b is near the upper bound of what the hardware can run.
- **Fix:** Set generous timeouts on the client side. Tab5 voice-response timeout increased from 30s to 120s.
- **Prevention:** Budget at least 20s for the full voice pipeline on ARM64 with 4B-parameter models. Future improvements: streaming TTS (send audio chunks as sentences complete), wake-word pre-activation, or offloading LLM to a faster backend.

### 5. sudo tee heredoc over SSH
- **Date:** 2026-03-29
- **Symptom:** Nested heredocs with `sudo tee` appeared to succeed over SSH but produced empty or corrupt files.
- **Root Cause:** Shell quoting and heredoc delimiters interact badly when piped through SSH, especially with sudo. The inner heredoc gets evaluated by the wrong shell layer.
- **Fix:** Write the file locally first, `scp` it to the target, then `sudo cp` it into place.
- **Prevention:** Never use `sudo tee` with heredocs over SSH. Always stage the file locally and transfer it.

---

## Deployment Issues

### 6. systemd User= mismatch
- **Date:** 2026-03-29
- **Symptom:** PermissionError at startup; service wrote files to the wrong home directory.
- **Root Cause:** systemd unit files had `User=rock` but the actual system user is `radxa`. The rock user was from an earlier OS image and no longer exists (or has no relevant files).
- **Fix:** Updated all systemd unit files to `User=radxa` with matching `WorkingDirectory=/home/radxa`.
- **Prevention:** After any OS re-image or user migration, run `grep -r 'User=' /etc/systemd/system/tinkerclaw*` and verify every entry. Add this check to the install script.

### 7. PYTHONPATH for module discovery
- **Date:** 2026-03-29
- **Symptom:** `ModuleNotFoundError: No module named 'dragon_voice'` when the systemd service started.
- **Root Cause:** The service runs `python3 -m dragon_voice` from `/home/radxa`, but Python's module search path did not include `/home/radxa` unless `PYTHONPATH` was set explicitly.
- **Fix:** Added `Environment=PYTHONPATH=/home/radxa` to the systemd unit file.
- **Prevention:** Any service that uses `python3 -m <package>` must have `PYTHONPATH` set to the directory containing the package in its systemd unit. Document this in the install script.

### 8. Stale services after architecture changes
- **Date:** 2026-03-29
- **Symptom:** Old `tinkerclaw-stream.service` was still loaded (disabled) after the streaming architecture was replaced.
- **Root Cause:** The service was disabled but never removed. `systemctl list-units` showed it as loaded, causing confusion during debugging.
- **Fix:** `sudo systemctl disable --now tinkerclaw-stream.service && sudo rm /etc/systemd/system/tinkerclaw-stream.service && sudo systemctl daemon-reload`.
- **Prevention:** When deprecating a service, always remove the unit file and daemon-reload. Keep a list of active services in the repo README and update it on architecture changes.

### 9. mDNS via avahi-publish
- **Date:** 2026-03-29
- **Symptom:** Tab5 could not discover Dragon on the LAN without a hardcoded IP.
- **Root Cause:** No mDNS service advertisement was configured.
- **Fix:** Created `tinkerclaw-mdns.service` that runs `avahi-publish-service "TinkerClaw Dragon" _tinkerclaw._tcp 3500` (dashboard port). Must specify the correct port number.
- **Prevention:** Any new network service that Tab5 needs to discover should be added to the avahi-publish command or get its own mDNS advertisement.

---

## Audio Pipeline Lessons

### 10. TTS sample rate mismatch
- **Date:** 2026-03-29
- **Symptom:** TTS audio played back at approximately 3x speed on the Tab5 speaker -- chipmunk voice.
- **Root Cause:** Piper TTS outputs 16kHz PCM natively. The Tab5 I2S bus runs at 48kHz. Without resampling, the DAC clocks out 16kHz samples at 48kHz, tripling the playback speed.
- **Fix:** Tab5 firmware performs 16kHz to 48kHz upsampling with linear interpolation before writing to I2S.
- **Prevention:** Always document the sample rate of every audio source and sink. Add a sample-rate assertion at the boundary between network receive and I2S write.

### 11. Voice pipeline latency budget
- **Date:** 2026-03-29
- **Symptom:** Users wait a noticeable amount of time between speaking and hearing a response.
- **Root Cause:** The full pipeline is sequential: Moonshine STT (~5s) + Ollama gemma3:4b (~12s) + Piper TTS (~3s) = ~20s total on ARM64.
- **Fix:** No immediate fix; this is a hardware constraint. Timeouts on Tab5 set to 120s to avoid premature disconnection.
- **Prevention:** Future improvements: streaming TTS (send audio chunks as sentences complete), wake-word to hide startup latency, faster/smaller LLM models, or offloading inference to a more powerful backend.

### 12. Voice server binary protocol (WebSocket)
- **Date:** 2026-03-29
- **Symptom:** Tab5 client only handled text frames and dropped audio data silently.
- **Root Cause:** The voice server sends two frame types over the same WebSocket: JSON text frames (status updates, transcription results) and binary frames (16kHz 16-bit mono PCM for TTS playback). The client must distinguish between them.
- **Fix:** Tab5 WebSocket handler checks frame type: text frames are parsed as JSON, binary frames are fed to the audio resampler and I2S output.
- **Prevention:** Document the wire protocol explicitly. Both sides must agree on frame types. Consider adding a 4-byte header to binary frames for future extensibility (e.g., sample rate, channel count).

---

## Architecture Decisions

### 13. Separate ports for services
- **Date:** 2026-03-29
- **Symptom:** N/A (design decision).
- **Root Cause:** Running all services behind a single port would couple their lifecycles and complicate debugging.
- **Fix:** Dragon CDP on port 3501, Voice on port 3502, Dashboard on port 3500. Each is an independent process.
- **Prevention:** Maintain the port registry in this document. New services get the next available port in the 35xx range.

### 14. dragon_voice moved from TinkerTab to TinkerBox
- **Date:** 2026-03-29
- **Symptom:** dragon_voice code was in the TinkerTab repo (ESP32 firmware), but it runs on Dragon (ARM64).
- **Root Cause:** Early development put everything in one repo. As the architecture matured, the voice server clearly belonged on the Dragon side.
- **Fix:** Moved `dragon_voice/` package to TinkerBox with proper subpackage structure: `stt/`, `tts/`, `llm/` backend subdirectories.
- **Prevention:** Code runs where it is deployed. Dragon-side code lives in TinkerBox, Tab5 firmware lives in TinkerTab. If unsure, ask: "What CPU executes this?"

### 15. Config hot-swap via dashboard
- **Date:** 2026-03-29
- **Symptom:** Changing STT/TTS/LLM backends required restarting the voice service.
- **Root Cause:** Configuration was only read at startup.
- **Fix:** Dashboard `POST /api/voice-config` proxies to the voice server, which reloads the pipeline without a restart. Allows switching backends at runtime.
- **Prevention:** Any new configurable parameter should be added to the hot-swap config endpoint, not require a service restart.

### 16. CDP port standardization (9222)
- **Date:** 2026-03-29
- **Symptom:** Confusion when connecting Chrome DevTools or automation scripts to Dragon's Chromium instance.
- **Root Cause:** The CDP port was originally set to 18800 (arbitrary), which conflicted with the conventional Chrome DevTools port.
- **Fix:** Changed to port 9222, the Chrome default, for consistency with the Chrome DevTools ecosystem.
- **Prevention:** Use well-known default ports whenever possible. Document any non-standard port choices in README and this file.

---

## Security / Secrets

### 17. No authentication on services
- **Date:** 2026-03-29
- **Symptom:** All Dragon services are accessible without any authentication.
- **Root Cause:** Design decision for LAN-only operation. All services listen on 0.0.0.0 without auth.
- **Fix:** Acceptable for LAN-only use. A `secrets.yaml` pattern is ready but not enforced.
- **Prevention:** Before exposing any service to the internet, implement API key authentication at minimum. The `secrets.yaml` pattern is in place; enforce it via middleware before any port forwarding or tunnel is set up.

### 18. secrets.yaml pattern
- **Date:** 2026-03-29
- **Symptom:** API keys (OpenRouter, LMStudio) were hardcoded or scattered across config files.
- **Root Cause:** No standard location for secrets.
- **Fix:** API keys go in `secrets.yaml` (gitignored, `chmod 600`). An example file is committed as `secrets.yaml.example` showing the expected structure.
- **Prevention:** Never commit real keys. CI or install scripts should check for `secrets.yaml` and fail with a clear message if it is missing.

---

## Cross-cutting (TinkerTab <-> TinkerBox)

### 19. I2S TDM bus architecture
- **Date:** 2026-03-29
- **Symptom:** Audio artifacts, clicks, or silence when DAC and ADC were configured independently.
- **Root Cause:** Tab5 uses a TDM 4-slot configuration on a shared I2S bus for both the ES8388 DAC and ES7210 ADC. Both TX and RX must use TDM mode for a consistent BCLK. Mixing standard I2S and TDM on the same bus causes clock conflicts.
- **Fix:** Configured both TX and RX channels as TDM with matching slot counts and bit widths.
- **Prevention:** On shared I2S buses, always configure TX and RX identically. Document the bus topology (which codecs share which I2S peripheral) in the hardware notes.

### 20. 48kHz to 16kHz downsample for STT
- **Date:** 2026-03-29
- **Symptom:** STT accuracy was poor when fed raw 48kHz audio.
- **Root Cause:** Moonshine STT expects 16kHz input. Feeding 48kHz data without downsampling produces garbage transcriptions.
- **Fix:** Tab5 firmware performs 3:1 decimation (takes every 3rd sample) before sending audio to Dragon.
- **Prevention:** No anti-alias filter is applied because the speech band (300Hz-4kHz) is well below the 8kHz Nyquist limit of 16kHz sampling. If non-speech audio processing is ever needed, add a low-pass filter before decimation.

### 21. ESP32-P4 PSRAM vs internal RAM
- **Date:** 2026-03-29
- **Symptom:** Boot crash or heap exhaustion when large static buffers were declared.
- **Root Cause:** The ESP32-P4 has 32MB PSRAM but only ~512KB internal SRAM. Large arrays declared as static BSS consume internal RAM. Anything over a few KB should be heap-allocated from PSRAM.
- **Fix:** Replaced large static arrays with `heap_caps_malloc(size, MALLOC_CAP_SPIRAM)` calls.
- **Prevention:** Never declare large static buffers in ESP32-P4 code. Use `MALLOC_CAP_SPIRAM` for anything over 4KB. Add a startup check that logs free internal vs PSRAM heap to catch regressions early.

### 22. WebSocket connection to port 3502 from Tab5 (UNRESOLVED)
- **Date:** 2026-03-29
- **Symptom:** Tab5 can connect to Dragon on port 3501 (CDP) but NOT port 3502 (voice server). No TCP connection reaches the voice server.
- **Root Cause:** Unknown. The voice server is confirmed listening on `0.0.0.0:3502` and is accessible from the workstation (curl, browser, wscat all connect fine). Only the ESP32-P4 fails to connect. Suspected causes: ESP-IDF WebSocket transport bug, DNS/host resolution difference between ports, or a subtle socket option mismatch.
- **Fix:** Still investigating. Workarounds under consideration: reverse proxy through port 3501, use raw TCP instead of WebSocket, or test with a different ESP-IDF WebSocket client library.
- **Prevention:** When adding a new network service, always test connectivity from the ESP32 client immediately -- do not assume that "if one port works, they all work."
