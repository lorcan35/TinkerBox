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

### 5. Ollama Extremely Slow on Radxa Zero 3W
- **Date:** 2026-03-29
- **Symptom:** gemma3:4b produces ~0.24 tok/s (33s for 8 tokens). Full voice cycle exceeds 60s.
- **Root Cause:** ARM64 CPU-only inference with 4.3B parameter model on 4GB RAM device
- **Fix:** No immediate fix — this is a hardware limitation. Tab5 timeout removed to accommodate. Consider lighter models or remote API offload.
- **Prevention:** Benchmark models before deploying. Consider llama3.2:1b or remote endpoints for faster response.

### 6. sudo tee heredoc over SSH
- **Date:** 2026-03-29
- **Symptom:** Nested heredocs with `sudo tee` appeared to succeed over SSH but produced empty or corrupt files.
- **Root Cause:** Shell quoting and heredoc delimiters interact badly when piped through SSH, especially with sudo. The inner heredoc gets evaluated by the wrong shell layer.
- **Fix:** Write the file locally first, `scp` it to the target, then `sudo cp` it into place.
- **Prevention:** Never use `sudo tee` with heredocs over SSH. Always stage the file locally and transfer it.

---

## Deployment Issues

### 7. systemd User= mismatch
- **Date:** 2026-03-29
- **Symptom:** PermissionError at startup; service wrote files to the wrong home directory.
- **Root Cause:** systemd unit files had `User=rock` but the actual system user is `radxa`. The rock user was from an earlier OS image and no longer exists (or has no relevant files).
- **Fix:** Updated all systemd unit files to `User=radxa` with matching `WorkingDirectory=/home/radxa`.
- **Prevention:** After any OS re-image or user migration, run `grep -r 'User=' /etc/systemd/system/tinkerclaw*` and verify every entry. Add this check to the install script.

### 8. PYTHONPATH for module discovery
- **Date:** 2026-03-29
- **Symptom:** `ModuleNotFoundError: No module named 'dragon_voice'` when the systemd service started.
- **Root Cause:** The service runs `python3 -m dragon_voice` from `/home/radxa`, but Python's module search path did not include `/home/radxa` unless `PYTHONPATH` was set explicitly.
- **Fix:** Added `Environment=PYTHONPATH=/home/radxa` to the systemd unit file.
- **Prevention:** Any service that uses `python3 -m <package>` must have `PYTHONPATH` set to the directory containing the package in its systemd unit. Document this in the install script.

### 9. Stale services after architecture changes
- **Date:** 2026-03-29
- **Symptom:** Old `tinkerclaw-stream.service` was still loaded (disabled) after the streaming architecture was replaced.
- **Root Cause:** The service was disabled but never removed. `systemctl list-units` showed it as loaded, causing confusion during debugging.
- **Fix:** `sudo systemctl disable --now tinkerclaw-stream.service && sudo rm /etc/systemd/system/tinkerclaw-stream.service && sudo systemctl daemon-reload`.
- **Prevention:** When deprecating a service, always remove the unit file and daemon-reload. Keep a list of active services in the repo README and update it on architecture changes.

### 10. mDNS via avahi-publish
- **Date:** 2026-03-29
- **Symptom:** Tab5 could not discover Dragon on the LAN without a hardcoded IP.
- **Root Cause:** No mDNS service advertisement was configured.
- **Fix:** Created `tinkerclaw-mdns.service` that runs `avahi-publish-service "TinkerClaw Dragon" _tinkerclaw._tcp 3500` (dashboard port). Must specify the correct port number.
- **Prevention:** Any new network service that Tab5 needs to discover should be added to the avahi-publish command or get its own mDNS advertisement.

### 11. ESP-IDF WS Ping Breaks aiohttp
- **Date:** 2026-03-29
- **Symptom:** aiohttp logs "Received fragmented control frame" and closes WS connection
- **Root Cause:** ESP32 esp_transport_ws sends WS ping as fragmented frame (spec violation)
- **Fix:** Tab5 sends `{"type":"ping"}` JSON text instead. Dragon server logs "Unknown command: ping" but stays connected.
- **Prevention:** Accept application-level heartbeats on the server side. Consider adding explicit ping handler.

---

## Audio Pipeline Lessons

### 12. TTS sample rate mismatch
- **Date:** 2026-03-29
- **Symptom:** TTS audio played back at approximately 3x speed on the Tab5 speaker -- chipmunk voice.
- **Root Cause:** Piper TTS outputs 16kHz PCM natively. The Tab5 I2S bus runs at 48kHz. Without resampling, the DAC clocks out 16kHz samples at 48kHz, tripling the playback speed.
- **Fix:** Tab5 firmware performs 16kHz to 48kHz upsampling with linear interpolation before writing to I2S.
- **Prevention:** Always document the sample rate of every audio source and sink. Add a sample-rate assertion at the boundary between network receive and I2S write.

### 13. Voice pipeline latency budget
- **Date:** 2026-03-29
- **Symptom:** Users wait a noticeable amount of time between speaking and hearing a response.
- **Root Cause:** The full pipeline is sequential: Moonshine STT (~5s) + Ollama gemma3:4b (~12s) + Piper TTS (~3s) = ~20s total on ARM64.
- **Fix:** No immediate fix; this is a hardware constraint. Timeouts on Tab5 set to 120s to avoid premature disconnection.
- **Prevention:** Future improvements: streaming TTS (send audio chunks as sentences complete), wake-word to hide startup latency, faster/smaller LLM models, or offloading inference to a more powerful backend.

### 14. Voice server binary protocol (WebSocket)
- **Date:** 2026-03-29
- **Symptom:** Tab5 client only handled text frames and dropped audio data silently.
- **Root Cause:** The voice server sends two frame types over the same WebSocket: JSON text frames (status updates, transcription results) and binary frames (16kHz 16-bit mono PCM for TTS playback). The client must distinguish between them.
- **Fix:** Tab5 WebSocket handler checks frame type: text frames are parsed as JSON, binary frames are fed to the audio resampler and I2S output.
- **Prevention:** Document the wire protocol explicitly. Both sides must agree on frame types. Consider adding a 4-byte header to binary frames for future extensibility (e.g., sample rate, channel count).

---

## Architecture Decisions

### 15. Separate ports for services
- **Date:** 2026-03-29
- **Symptom:** N/A (design decision).
- **Root Cause:** Running all services behind a single port would couple their lifecycles and complicate debugging.
- **Fix:** Dragon CDP on port 3501, Voice on port 3502, Dashboard on port 3500. Each is an independent process.
- **Prevention:** Maintain the port registry in this document. New services get the next available port in the 35xx range.

### 16. dragon_voice moved from TinkerTab to TinkerBox
- **Date:** 2026-03-29
- **Symptom:** dragon_voice code was in the TinkerTab repo (ESP32 firmware), but it runs on Dragon (ARM64).
- **Root Cause:** Early development put everything in one repo. As the architecture matured, the voice server clearly belonged on the Dragon side.
- **Fix:** Moved `dragon_voice/` package to TinkerBox with proper subpackage structure: `stt/`, `tts/`, `llm/` backend subdirectories.
- **Prevention:** Code runs where it is deployed. Dragon-side code lives in TinkerBox, Tab5 firmware lives in TinkerTab. If unsure, ask: "What CPU executes this?"

### 17. Config hot-swap via dashboard
- **Date:** 2026-03-29
- **Symptom:** Changing STT/TTS/LLM backends required restarting the voice service.
- **Root Cause:** Configuration was only read at startup.
- **Fix:** Dashboard `POST /api/voice-config` proxies to the voice server, which reloads the pipeline without a restart. Allows switching backends at runtime.
- **Prevention:** Any new configurable parameter should be added to the hot-swap config endpoint, not require a service restart.

### 18. CDP port standardization (9222)
- **Date:** 2026-03-29
- **Symptom:** Confusion when connecting Chrome DevTools or automation scripts to Dragon's Chromium instance.
- **Root Cause:** The CDP port was originally set to 18800 (arbitrary), which conflicted with the conventional Chrome DevTools port.
- **Fix:** Changed to port 9222, the Chrome default, for consistency with the Chrome DevTools ecosystem.
- **Prevention:** Use well-known default ports whenever possible. Document any non-standard port choices in README and this file.

---

## Security / Secrets

### 19. No authentication on services
- **Date:** 2026-03-29
- **Symptom:** All Dragon services are accessible without any authentication.
- **Root Cause:** Design decision for LAN-only operation. All services listen on 0.0.0.0 without auth.
- **Fix:** Acceptable for LAN-only use. A `secrets.yaml` pattern is ready but not enforced.
- **Prevention:** Before exposing any service to the internet, implement API key authentication at minimum. The `secrets.yaml` pattern is in place; enforce it via middleware before any port forwarding or tunnel is set up.

### 20. secrets.yaml pattern
- **Date:** 2026-03-29
- **Symptom:** API keys (OpenRouter, LMStudio) were hardcoded or scattered across config files.
- **Root Cause:** No standard location for secrets.
- **Fix:** API keys go in `secrets.yaml` (gitignored, `chmod 600`). An example file is committed as `secrets.yaml.example` showing the expected structure.
- **Prevention:** Never commit real keys. CI or install scripts should check for `secrets.yaml` and fail with a clear message if it is missing.

---

## Cross-cutting (TinkerTab <-> TinkerBox)

### 21. I2S TDM bus architecture
- **Date:** 2026-03-29
- **Symptom:** Audio artifacts, clicks, or silence when DAC and ADC were configured independently.
- **Root Cause:** Tab5 uses a TDM 4-slot configuration on a shared I2S bus for both the ES8388 DAC and ES7210 ADC. Both TX and RX must use TDM mode for a consistent BCLK. Mixing standard I2S and TDM on the same bus causes clock conflicts.
- **Fix:** Configured both TX and RX channels as TDM with matching slot counts and bit widths.
- **Prevention:** On shared I2S buses, always configure TX and RX identically. Document the bus topology (which codecs share which I2S peripheral) in the hardware notes.

### 22. 48kHz to 16kHz downsample for STT
- **Date:** 2026-03-29
- **Symptom:** STT accuracy was poor when fed raw 48kHz audio.
- **Root Cause:** Moonshine STT expects 16kHz input. Feeding 48kHz data without downsampling produces garbage transcriptions.
- **Fix:** Tab5 firmware performs 3:1 decimation (takes every 3rd sample) before sending audio to Dragon.
- **Prevention:** No anti-alias filter is applied because the speech band (300Hz-4kHz) is well below the 8kHz Nyquist limit of 16kHz sampling. If non-speech audio processing is ever needed, add a low-pass filter before decimation.

### 23. ESP32-P4 PSRAM vs internal RAM
- **Date:** 2026-03-29
- **Symptom:** Boot crash or heap exhaustion when large static buffers were declared.
- **Root Cause:** The ESP32-P4 has 32MB PSRAM but only ~512KB internal SRAM. Large arrays declared as static BSS consume internal RAM. Anything over a few KB should be heap-allocated from PSRAM.
- **Fix:** Replaced large static arrays with `heap_caps_malloc(size, MALLOC_CAP_SPIRAM)` calls.
- **Prevention:** Never declare large static buffers in ESP32-P4 code. Use `MALLOC_CAP_SPIRAM` for anything over 4KB. Add a startup check that logs free internal vs PSRAM heap to catch regressions early.

### 24. WebSocket connection to port 3502 from Tab5 (UNRESOLVED)
- **Date:** 2026-03-29
- **Symptom:** Tab5 can connect to Dragon on port 3501 (CDP) but NOT port 3502 (voice server). No TCP connection reaches the voice server.
- **Root Cause:** Unknown. The voice server is confirmed listening on `0.0.0.0:3502` and is accessible from the workstation (curl, browser, wscat all connect fine). Only the ESP32-P4 fails to connect. Suspected causes: ESP-IDF WebSocket transport bug, DNS/host resolution difference between ports, or a subtle socket option mismatch.
- **Fix:** Still investigating. Workarounds under consideration: reverse proxy through port 3501, use raw TCP instead of WebSocket, or test with a different ESP-IDF WebSocket client library.
- **Prevention:** When adding a new network service, always test connectivity from the ESP32 client immediately -- do not assume that "if one port works, they all work."

### 25. QAIRT SDK zip extraction fails with unzip
- **Date:** 2026-03-29
- **Symptom:** `unzip qairt-v2.37.1.zip` says "cannot find zipfile directory"
- **Root Cause:** The 1.3GB zip exceeds unzip's internal limits for large archives.
- **Fix:** Use `7z x` (from `p7zip-full` package) instead of `unzip`.
- **Prevention:** Always use `7z` for archives over 500MB.

### 26. QCS6490 NPU — V68 vs V73 confusion
- **Date:** 2026-03-29
- **Symptom:** `qnn-platform-validator --backend dsp --testBackend` fails looking for V68 calculator stub, even with V73 libs deployed.
- **Root Cause:** The platform validator hardcodes V68 as first probe target. The Radxa modelscope package (`radxa/Llama3.2-1B-4096-qairt-v68`) ships V68 libs and uses `dsp_arch: v68` in config, suggesting QCS6490 exposes HTP as V68 to userspace despite having V73 hardware.
- **Fix:** Use the bundled libs from the modelscope download, not the SDK V73 libs. The model package knows the correct HTP version for this SoC.
- **Prevention:** Always check the model package's `htp_backend_ext_config.json` for `dsp_arch` rather than assuming the HTP version from Qualcomm datasheets.

### 27. NPU Genie — 30x faster than Ollama on ARM64
- **Date:** 2026-03-29
- **Symptom:** Ollama gemma3:4b generates at ~0.24 tok/s on QCS6490 CPU — too slow for real-time voice.
- **Root Cause:** Ollama runs on CPU (ARM Cortex-A78) with no NPU offload. The QCS6490's Hexagon DSP is designed for exactly this workload.
- **Fix:** Installed QAIRT SDK + Llama 3.2 1B via `genie-t2t-run`. Achieves ~8 tok/s on NPU (HTP backend). ~110 tokens in ~13.6s generation time + ~2s model load.
- **Prevention:** Always prefer NPU inference on Qualcomm SoCs. CPU-only LLM inference on ARM64 is a last resort.

### 28. genie-t2t-run is stateless (new process per request)
- **Date:** 2026-03-29
- **Symptom:** Each genie-t2t-run invocation takes ~2s for model loading before generation begins.
- **Root Cause:** genie-t2t-run loads the full 1.66GB model from disk into shared memory on every invocation. There is no persistent server mode.
- **Fix:** Acceptable for now (~2s overhead on ~15s total). Future optimization: write a persistent Genie server that keeps the model loaded in memory.
- **Prevention:** Factor in cold-start latency when benchmarking NPU inference. Report total time (load+generate) and generation-only time separately.

### 29. QCS6490 HTP v68 limits NPU models to 1B parameter class
- **Date:** 2026-03-29
- **Symptom:** Wanted to run Llama 3.2 3B on NPU for better quality. Dragon has 12GB RAM — plenty for 3B weights (~2.5GB).
- **Root Cause:** QCS6490 Hexagon DSP presents as HTP v68. Genie context binaries (`.serialized.bin`) are compiled for a specific HTP instruction set architecture and are NOT cross-compatible between versions. All available 3B quantized models target v73+ (Snapdragon 8 Gen 2 and newer). Sources checked: HuggingFace Volko76 (v73 only), Radxa ModelScope (1B only for v68), Qualcomm AI Hub (QCS6490 not a supported target for 3B export).
- **Fix:** Stay with Llama 3.2 1B on NPU (~8 tok/s). The blocker is HTP architecture, not RAM.
- **Prevention:** When evaluating Qualcomm SoCs for LLM inference, check the HTP version (v68/v73/v75/v79), not just RAM. The HTP arch determines which pre-quantized models are available. v73+ (Snapdragon 8 Gen 2+) is the minimum for 3B+ models.
