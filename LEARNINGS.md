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

### 29. Multi-turn conversation via ConversationEngine
- **Date:** 2026-03-30
- **Symptom:** N/A (new feature).
- **Root Cause:** Voice pipeline originally had no conversation memory — each utterance was stateless. Users could not have multi-turn dialogues.
- **Fix:** Created `ConversationEngine` in `conversation.py` backed by `MessageStore` and `Database`. All messages (user + assistant) are stored in SQLite and the last N messages are loaded as OpenAI-format context for every LLM call. Works identically for voice (post-STT) and text (keyboard/API) input.
- **Prevention:** Any new input modality must route through ConversationEngine to maintain context. Never call the LLM directly from a handler — always go through the engine.

### 30. Session resume across WebSocket disconnects
- **Date:** 2026-03-30
- **Symptom:** Disconnecting and reconnecting (Wi-Fi drop, Tab5 sleep, etc.) started a fresh conversation with no history.
- **Root Cause:** Sessions were tied to the WebSocket connection lifetime. No persistence layer.
- **Fix:** `SessionManager` in `sessions.py` implements create/resume/pause/end lifecycle. On disconnect, session status goes to `paused` (not `ended`). On reconnect, Tab5 sends the previous `session_id` in the `register` message and Dragon resumes the session with full message history intact. Auto-cleanup task ends stale sessions after 30 minutes of inactivity.
- **Prevention:** Session state must always be in the database, never in-memory only. The WebSocket connection is a transport — session lifecycle is independent.

### 31. aiosqlite for async database access
- **Date:** 2026-03-30
- **Symptom:** Synchronous sqlite3 calls would block the aiohttp event loop during DB writes, causing audio dropouts and increased latency.
- **Root Cause:** Python's `sqlite3` module is synchronous. The voice server is fully async (aiohttp).
- **Fix:** Used `aiosqlite` with WAL journal mode for non-blocking reads and writes. All DB access goes through a single `Database` class in `db.py` — no raw SQL elsewhere.
- **Prevention:** Never use synchronous I/O in the voice server. All file and database operations must be async or run in a thread pool.

### 32. NPU Genie cold start latency
- **Date:** 2026-03-30
- **Symptom:** First genie-t2t-run invocation after boot takes significantly longer than subsequent calls.
- **Root Cause:** The Hexagon DSP runtime and shared memory mappings are initialized on first use. The 1.66GB model must be loaded from eMMC into shared memory. Subsequent calls within the same session still re-load (genie-t2t-run is stateless per invocation) but benefit from filesystem cache.
- **Fix:** Acceptable for now. The ~2s model load per call (see #28) is the dominant overhead. A persistent Genie server process would eliminate this entirely.
- **Prevention:** When benchmarking NPU performance, always discard the first cold-start measurement. Report warm-start latency as the representative number.

### 33. QCS6490 HTP v68 limits NPU models to 1B parameter class
- **Date:** 2026-03-29
- **Symptom:** Wanted to run Llama 3.2 3B on NPU for better quality. Dragon has 12GB RAM — plenty for 3B weights (~2.5GB).
- **Root Cause:** QCS6490 Hexagon DSP presents as HTP v68. Genie context binaries (`.serialized.bin`) are compiled for a specific HTP instruction set architecture and are NOT cross-compatible between versions. All available 3B quantized models target v73+ (Snapdragon 8 Gen 2 and newer). Sources checked: HuggingFace Volko76 (v73 only), Radxa ModelScope (1B only for v68), Qualcomm AI Hub (QCS6490 not a supported target for 3B export).
- **Fix:** Stay with Llama 3.2 1B on NPU (~8 tok/s). The blocker is HTP architecture, not RAM.
- **Prevention:** When evaluating Qualcomm SoCs for LLM inference, check the HTP version (v68/v73/v75/v79), not just RAM. The HTP arch determines which pre-quantized models are available. v73+ (Snapdragon 8 Gen 2+) is the minimum for 3B+ models.

---

## Session Bugs (2026-04-06)

### 34. OpenRouter API key ${env:...} not expanded
- **Date:** 2026-04-06
- **Symptom:** OpenRouter API calls failed with authentication errors. The API key was literally `${env:OPENROUTER_API_KEY}` instead of the actual key value.
- **Root Cause:** The YAML config used a literal string (single-quoted or block scalar) for the API key field, which bypassed the environment variable expansion/fallback logic in `config.py`. The `${env:...}` syntax was treated as a plain string, not a variable reference.
- **Fix:** Changed the API key config value to an empty string, which triggers the env var fallback path in the config loader to read `OPENROUTER_API_KEY` from the environment.
- **Prevention:** Test env var expansion for every secret in config.yaml. Never use YAML literal strings (`'...'` or `|`) for values that need variable substitution. Add a startup check that validates API keys are non-empty and don't contain literal `${` characters.

### 35. LLM memory leak across sessions (OpenRouterBackend._conversation)
- **Date:** 2026-04-06
- **Symptom:** Memory usage grew steadily over time. After many voice sessions, Dragon became sluggish and eventually ran out of memory.
- **Root Cause:** `OpenRouterBackend._conversation` list accumulated messages across all sessions and was never cleared. When `ConversationEngine` created a new session, the LLM backend still held the entire history from all previous sessions in memory.
- **Fix:** Added a `generate_stream_with_messages()` override to the OpenRouter backend that accepts an explicit message list per call, bypassing the stale `_conversation` accumulator. The conversation context is now built fresh from the database by `ConversationEngine` on each request.
- **Prevention:** LLM backends must not maintain their own conversation state. Context should be built per-request from the authoritative source (database). Audit all backend classes for internal message accumulation.

### 36. Clear command only clears in-memory LLM history, not DB
- **Date:** 2026-04-06
- **Symptom:** After using the "clear" command, old messages reappeared when the session was resumed or the service restarted. The conversation was not actually cleared.
- **Root Cause:** The clear command only reset the in-memory `_conversation` list in the LLM backend. It did not end the `ConversationEngine` session or clear messages from the SQLite database. On next request, the engine reloaded the full history from DB.
- **Fix:** Clear command now ends the current session (marking it `ended` in DB) and creates a new session. This gives a clean slate — the old messages still exist in the database for history, but the new session starts with no context.
- **Prevention:** Any "reset" or "clear" operation must go through `SessionManager` lifecycle methods (end + create), not bypass them by clearing in-memory state. Test clear by restarting the service and verifying the old context does not return.

### 37. Text TTS failure leaves Tab5 stuck in PROCESSING
- **Date:** 2026-04-06
- **Symptom:** After a TTS error during text-to-speech, Tab5 remained stuck in PROCESSING state indefinitely. No further voice interactions were possible without rebooting.
- **Root Cause:** When the TTS backend raised an exception, the error handler did not send a `tts_end` message to Tab5. Tab5 was waiting for `tts_end` to transition back to READY state, but it never arrived.
- **Fix:** Added `tts_end` message send in the exception handler, ensuring Tab5 always receives the end-of-TTS signal regardless of whether TTS succeeded or failed.
- **Prevention:** Every code path that can start a TTS stream (sending `tts_start`) must guarantee a corresponding `tts_end` is sent, even on failure. Use try/finally to ensure this. Add a watchdog on Tab5 that auto-recovers if `tts_end` is not received within a reasonable timeout.

### 38. Cloud TTS wrong format (gpt-audio-mini)
- **Date:** 2026-04-06
- **Symptom:** Cloud TTS via gpt-audio-mini returned errors or garbled audio. Tab5 received data it could not play.
- **Root Cause:** The cloud TTS request was using `stream=false` and requesting `wav` format. The gpt-audio-mini model requires `stream=true` and `format=pcm16` to produce correct streaming audio output.
- **Fix:** Changed the cloud TTS request parameters to `stream=true` and `format=pcm16`.
- **Prevention:** Always check the specific API documentation for each TTS model. Do not assume request parameters are interchangeable between local (Piper) and cloud (gpt-audio-mini) backends. Add backend-specific parameter validation.

### 39. Cloud STT bad prompt causing transcription errors
- **Date:** 2026-04-06
- **Symptom:** Cloud STT returned inaccurate or nonsensical transcriptions, especially for short utterances.
- **Root Cause:** The STT prompt was a generic string like "transcribe" which confused the model. Cloud STT models use the prompt as context/guidance for what to expect in the audio.
- **Fix:** Improved the STT prompt to provide better context about the expected audio content (conversational speech, voice assistant interaction).
- **Prevention:** STT prompts should describe the expected audio context, not be generic commands. Test transcription quality with realistic utterances whenever changing the prompt.

### 40. TTS pacing — multiple tts_start/tts_end per utterance
- **Date:** 2026-04-06
- **Symptom:** TTS audio had gaps and clicks. Parts of sentences were cut off or replayed. Tab5 speaker produced choppy output.
- **Root Cause:** The server sent separate `tts_start`/`tts_end` pairs for each sentence or chunk within a single response. Each `tts_start` caused Tab5 to reset its audio buffer, dropping any audio that was still playing from the previous chunk.
- **Fix:** Changed to a single `tts_start` at the beginning of the response and a single `tts_end` at the end. Audio chunks are streamed between them with 80% real-time pacing (slight delay between chunks to prevent buffer underrun without causing noticeable latency).
- **Prevention:** TTS framing must be one `tts_start` / `tts_end` pair per complete response, never per sentence. Pacing should be configurable. Document the expected framing protocol in `docs/protocol.md`.

### 41. Dragon error transitions Tab5 to IDLE instead of READY
- **Date:** 2026-04-06
- **Symptom:** After a transient Dragon error (e.g., LLM timeout), Tab5 showed "Disconnected" status and would not accept new voice input, even though the WebSocket was still connected.
- **Root Cause:** The error handler on Tab5 transitioned the state machine to IDLE (disconnected state) regardless of whether the WebSocket was still alive. Transient errors (LLM timeout, TTS failure) are not connection failures.
- **Fix:** Added a `ws_connected` check in the error handler. If the WebSocket is still connected, transition to READY (ready for new input) instead of IDLE (disconnected).
- **Prevention:** Distinguish between connection errors (transition to IDLE) and processing errors (transition to READY). Never use IDLE for transient failures when the transport is still alive.

### 42. Response timeout never fires (keepalive resets activity timer)
- **Date:** 2026-04-06
- **Symptom:** When the LLM hung or Dragon stopped responding, Tab5 waited indefinitely instead of timing out and recovering.
- **Root Cause:** The keepalive ping was sent every 15 seconds. The response timeout was 20 seconds. Each keepalive ping reset the activity timer, so the 20-second timeout could never be reached — it was reset to 0 every 15 seconds by the keepalive.
- **Fix:** Separated the keepalive timer (connection liveness, sends pings) from the response activity timer (tracks time since last meaningful response data). Keepalive pings no longer reset the activity timer.
- **Prevention:** Keepalive and response timeout are orthogonal concerns. Keepalive checks transport liveness. Response timeout checks application progress. Never let one reset the other. This same bug was also found and fixed on Tab5 (see TinkerTab LEARNINGS #41).

### 43. Ollama generation has no timeout
- **Date:** 2026-04-06
- **Symptom:** Occasionally the voice pipeline hung forever waiting for Ollama to respond. No error, no timeout — just infinite wait.
- **Root Cause:** The Ollama HTTP client call had no timeout configured. If Ollama entered a bad state (deadlock, OOM, etc.), the `await` would never resolve.
- **Fix:** Added a 120-second timeout to the Ollama generation call. If exceeded, the call raises a timeout exception which is caught by the pipeline error handler and reported to Tab5 as an error.
- **Prevention:** Every external service call (HTTP, subprocess, WebSocket) must have an explicit timeout. Default to 120s for LLM generation, 30s for STT/TTS, 10s for health checks. Never use `await` without a timeout on external I/O.

### 44. Ping handler was a no-op (Tab5 heartbeat ignored)
- **Date:** 2026-04-06
- **Symptom:** Tab5 heartbeat pings were received by Dragon but produced no response. During network instability, Tab5 could not determine if Dragon was still alive.
- **Root Cause:** The WebSocket message handler recognized `{"type":"ping"}` messages but did nothing with them — no pong response was sent. The handler was a silent no-op.
- **Fix:** Added a pong response: when Dragon receives `{"type":"ping"}`, it immediately sends `{"type":"pong"}` back to Tab5.
- **Prevention:** Every request-type message in the protocol must have a defined response. Add ping/pong to `docs/protocol.md` as a required message pair. Test heartbeat round-trip in integration tests.

---

## Agentic Pipeline Bugs (2026-04-07)

### 45. Shared conversation callbacks race condition
- **Date:** 2026-04-07
- **Symptom:** When multiple WebSocket clients were connected simultaneously, tool event callbacks (tool_call, tool_result) were delivered to the wrong client or lost entirely. Only the most recently connected client received tool events.
- **Root Cause:** `ConversationEngine` was a shared singleton, but the `on_tool_call` and `on_tool_result` callbacks were set as instance attributes per-connection. Each new WebSocket connection overwrote the previous callbacks — last-writer-wins. Earlier connections lost their callback references.
- **Fix:** Removed callback storage from ConversationEngine. Instead, pass `on_tool_call` and `on_tool_result` as parameters to `process_text_stream()` on every call. Each connection provides its own callbacks at call time — no shared mutable state.
- **Prevention:** Never store per-connection state on shared/singleton objects. Pass connection-scoped callbacks as function parameters, not as object attributes. Review all shared engine classes for per-connection state leaks.

### 46. Double-store bug in tool-calling
- **Date:** 2026-04-07
- **Symptom:** When the LLM made a tool call, the assistant message appeared twice in the conversation history. The database had duplicate entries for the same response.
- **Root Cause:** During tool execution, the assistant response (containing the tool call markers) was stored in the database immediately. Then, after the tool result was injected and the LLM continued, the final response was stored again at the end of the pipeline. The initial partial response was never cleaned up.
- **Fix:** Only store the final complete response at the end of `process_text_stream()`. Removed the intermediate store that happened during tool call parsing. Tool call/result messages are stored separately as their own message types.
- **Prevention:** Assistant responses should be stored exactly once — at the end of the full generation cycle (including all tool calls). Never store intermediate/partial responses. Add a unique constraint or dedup check if multiple store paths exist.

### 47. numpy module-level import crash
- **Date:** 2026-04-07
- **Symptom:** Voice server failed to start with `ModuleNotFoundError: No module named 'numpy'`. The entire service was down.
- **Root Cause:** `synthesize.py` (the TTS synthesis API route) imported `numpy` at module level (`import numpy as np` at the top of the file). When `numpy` was not installed on Dragon (common on minimal ARM64 installs), the import failed at module load time, preventing the entire `api/` package from initializing.
- **Fix:** Moved the `numpy` import inside the function that actually uses it (lazy import). The module loads successfully even without numpy — the specific route that needs numpy will raise an error only if called.
- **Prevention:** Never import optional/heavy dependencies at module level in server code. Use lazy imports inside the functions that need them. This ensures the server starts even if an optional dependency is missing — only the specific feature that requires it will fail gracefully.

### 48. Old api.py dead code
- **Date:** 2026-04-07
- **Symptom:** Confusion during debugging — edits to `dragon_voice/api.py` had no effect because the server was actually loading routes from `dragon_voice/api/__init__.py` (the package).
- **Root Cause:** After refactoring the monolithic `api.py` file into the `api/` package (with `__init__.py`, `sessions.py`, `messages.py`, etc.), the old `api.py` file was left behind. Python's module resolution found the `api/` package first, but the leftover file caused confusion when reading or searching the codebase.
- **Fix:** Deleted the old `dragon_voice/api.py` file. Only the `api/` package directory remains.
- **Prevention:** When refactoring a module into a package, always delete the original file in the same commit. Verify with `git status` that no orphaned files remain. Add a CI check that flags .py files at the same path as a package directory.

### 49. config.yaml overwritten on deploy
- **Date:** 2026-04-07
- **Symptom:** After deploying code to Dragon via `scp -r dragon_voice/`, Dragon's local config was overwritten. Custom settings (API keys, backend selections, local paths) were replaced with development defaults.
- **Root Cause:** The `scp -r dragon_voice/` command copies the entire directory including `config.yaml`. The source repo's `config.yaml` had `backend: openrouter` as the default LLM backend, which overwrote Dragon's local config that had `backend: ollama` (the correct default for the ARM64 hardware).
- **Fix:** Changed the default `backend` in the source `config.yaml` to `ollama` so that even if the file is overwritten during deploy, Dragon gets a safe default that works without cloud API keys.
- **Prevention:** Default config values in the repo should always be the safest/most-compatible option (local backends, no API keys required). Consider adding `config.yaml` to a deploy exclude list, or use a `config.local.yaml` overlay pattern where local overrides are never touched by deploy.

---

## TinkerClaw Sidecar Integration (2026-04-14)

### 50. TinkerClaw sidecar — voice mode 3 bypasses Dragon intelligence
- **Date:** 2026-04-14
- **Symptom:** N/A (new feature — voice mode 3 added).
- **Root Cause:** Dragon's local LLM (1B on NPU) and cloud LLM (OpenRouter) have different tradeoffs — local is fast but limited, cloud is capable but adds latency and cost. TinkerClaw provides a third option: a full agent runner (with its own tools, memory, and skills) running as a sidecar on localhost.
- **Fix:** Added `tinkerclaw` LLM backend (`dragon_voice/llm/tinkerclaw_llm.py`). In voice mode 3, Dragon handles STT and TTS only — the transcript is forwarded to TinkerClaw gateway on port 18789 (localhost). ConversationEngine, ToolRegistry, and MemoryService are all bypassed; TinkerClaw owns the intelligence layer. Session continuity is maintained by passing Dragon's `session_id` as the `user` field. If the gateway is unreachable, Dragon sends an error to Tab5 and auto-reverts to Local mode (same fallback pattern as cloud modes).
- **Prevention:** The TinkerClaw backend must be treated as an optional dependency — Dragon must start and function normally without it. Never import tinkerclaw_llm.py at module level. The gateway health check (connect to 18789) must have a short timeout (2s) to avoid blocking the voice pipeline. Config lives in `~/.tinkerclaw/tinkerclaw.json`, separate from Dragon's `config.yaml`.

### 52. Context window overflow from message-count-only limiting (US-P16)
- **Date:** 2026-04-14
- **Symptom:** OpenRouter returns HTTP 400 "context_length_exceeded" during cloud LLM calls, crashing the session. Happens when conversation history has many long messages (e.g. 29 messages * 800 tokens = 23,200 tokens + system prompt + memory + tools).
- **Root Cause:** `_build_context()` in `conversation.py` limited history by message COUNT (10 local, 30 cloud), not by TOKEN count. A few long messages could easily exceed the model's context window.
- **Fix:** Added `estimate_tokens()` (len/4 approximation) and `trim_context_to_budget()` in `messages.py`. After building the full context (system prompt + memory + tools + history), `_build_context()` trims oldest non-system messages to stay within budget: 25,600 tokens for local (80% of 32K), 100,000 tokens for cloud (80% of 128K). Also added context_length_exceeded retry in `openrouter_llm.py` — if the API still rejects the context, it halves the budget and retries once.
- **Prevention:** Always enforce token budgets, not just message counts. The system prompt + memory + tools can grow unpredictably (tool results, long memory facts), so trimming must happen AFTER all augmentation is applied.

### 53. Piper TTS zombie process accumulation (US-P24)
- **Date:** 2026-04-14
- **Symptom:** After hours of use with frequent interruptions (user speaks mid-response), dozens of Piper subprocesses accumulate as zombies, consuming PIDs and memory.
- **Root Cause:** `_synthesize_binary()` used `subprocess.run()` which blocks in a thread pool. When the pipeline is cancelled (user interrupts), the asyncio task is cancelled but the thread running subprocess.run() continues until the process exits naturally. If the process hangs or takes long, it becomes orphaned.
- **Fix:** Replaced `subprocess.run()` with `subprocess.Popen()` + explicit tracking in `self._active_procs`. Each process is tracked and removed after completion. Added `kill_active_procs()` method called from both `pipeline.cancel()` (flush/interrupt) and `shutdown()`. Note: Piper supports a persistent server mode (`piper --server`) that avoids fork overhead entirely — documented as future optimization.
- **Prevention:** Never use untracked `subprocess.run()` in async code where cancellation can occur. Always use Popen with explicit lifecycle management. Track all child processes and kill them on cancel/shutdown.

### 51. ngrok WS stability — protocol pings not counted as activity
- **Date:** 2026-04-14
- **Symptom:** Dragon's aiohttp heartbeat killed WS connections through ngrok. Protocol pings were not counted as activity.
- **Root Cause:** (1) aiohttp `heartbeat=30s` sent WebSocket protocol pings; ESP-IDF auto-PONGed but ngrok latency caused timeouts. (2) ngrok only counts data frames as activity, not protocol ping/pong frames.
- **Fix:** Set `heartbeat=None` to disable aiohttp protocol pings. Added a `_ws_keepalive` task that sends `{"type":"pong"}` JSON data frames every 15 seconds. These are real data frames that ngrok counts as activity.
- **Prevention:** Through WS proxies (ngrok, Cloudflare), always use application-level JSON keepalive, never rely on WebSocket protocol pings. Protocol pings may not traverse proxies reliably and are invisible to proxy activity timers.

### 54. Mode switch during active speech corrupts audio pipeline (US-P01)
- **Date:** 2026-04-14
- **Symptom:** If a config_update (voice mode switch) arrives while audio is buffered or being processed, the in-flight PCM buffer is orphaned, the new STT backend receives no audio, and user speech is lost. In theory, if Moonshine is mid-inference when shutdown() is called, the C++ runtime could crash.
- **Root Cause:** Three gaps: (1) `feed_audio()` had no guard against backend swaps -- it continued appending PCM data during the swap window, creating stale audio for the new backend. (2) `swap_backends()` did not clear audio buffers after cancelling in-flight processing -- audio arriving between cancel() and swap completion accumulated. (3) The `config_update` handler did not acquire `conn_lock`, so it could race with `stop`/`text` handlers that also mutate pipeline state.
- **Fix:** (1) Added `_swapping` flag to pipeline -- `feed_audio()` drops all incoming audio while True. (2) `swap_backends()` now clears `_audio_buffer`, `_segment_buffer`, and `_is_speaking` after cancel and before initializing new backends, wrapped in try/finally to always clear `_swapping`. (3) The pipeline swap section of `config_update` in server.py now acquires `conn_lock` to serialize with stop/text handlers. Redundant cancel in server.py removed since `swap_backends()` handles it internally.
- **Prevention:** Any operation that tears down or replaces pipeline backends must set `_swapping=True` first and clear audio buffers. All pipeline-mutating WS commands must acquire `conn_lock`.

### 55. TinkerClaw SSE truncation silently sends partial response to TTS (A07)
- **Date:** 2026-04-14
- **Symptom:** When TinkerClaw crashes mid-SSE-stream (e.g., sqlite-vec segfault), Dragon's `async for line in resp.content` silently returns empty on connection close. Dragon treats the partial 800-token fragment as a complete response and sends it to TTS, which reads a mid-sentence fragment aloud.
- **Root Cause:** The SSE parser had no way to distinguish between a completed stream (which ends with `data: [DONE]`) and a truncated stream (which just closes the connection). Both caused the `async for` loop to exit normally.
- **Fix:** Added `saw_done` and `token_count` tracking in `generate_stream_with_messages()`. After the loop exits, if `[DONE]` was never received: (1) if tokens were received, append " ... (response interrupted)" and log a warning; (2) if zero tokens were received, yield the connection error message. Also raised `sock_read` timeout from 30s to 90s and `total` from 120s to 180s to accommodate TinkerClaw tool execution gaps (see #56).
- **Prevention:** Any SSE parser must track whether the stream terminated cleanly (`[DONE]`) vs. was interrupted. Never assume loop exit = complete response.

### 56. TinkerClaw tool-calling SSE gap causes timeout (P05)
- **Date:** 2026-04-14
- **Symptom:** When TinkerClaw's MiniMax M2.5 calls a tool (e.g., web_search), there is a 10-30s gap in the SSE stream while the tool executes. Dragon's `sock_read=30s` timeout could fire during this gap, killing the stream.
- **Root Cause:** TinkerClaw sends a SINGLE SSE stream for the entire agent run. `agentCommandFromIngress()` in `openai-http.ts` runs the full agent loop (tool calls + final response) as one async operation. The SSE event listener streams `assistant` deltas, but during tool execution no content deltas are emitted — only a gap. There is only ONE `[DONE]` at the very end (after lifecycle.end fires). The original fear of "intermediate [DONE] after tool_call" was unfounded.
- **Fix:** Raised `sock_read` from 30s to 90s and `total` from 120s to 180s. No parser changes needed — Dragon already correctly skips empty-content deltas (tool_call chunks have no `content` field) and picks up the final assistant text when it arrives. The `server.py` _handle_text keepalive task (10s ws.ping) already keeps the WebSocket alive during the gap.
- **Prevention:** When integrating with agent systems that execute tools server-side, SSE read timeouts must be set generously (>= max tool execution time). Document the expected SSE flow for each integration point.

### 57. Dragon memory growth over 24h causes OOM kill (A04)
- **Date:** 2026-04-14
- **Symptom:** After 24h of continuous voice conversations, Dragon's RSS grows from Moonshine/ONNX inference buffers, aiohttp connection pools, and Python GC not collecting circular references. Eventually the OOM killer fires.
- **Root Cause:** No memory monitoring or proactive cleanup. Python's GC doesn't always collect circular references promptly, and ONNX Runtime inference buffers accumulate.
- **Fix:** Added `_memory_monitor` periodic task (every 5 min) in `server.py`. Reads RSS from `/proc/self/status` (no psutil dependency). Logs RSS at INFO level. At 2GB warning threshold: forces `gc.collect()`. At 3GB critical threshold (after GC): gracefully shuts down and re-initializes all active pipelines, freeing Moonshine/ONNX buffers and connection pool memory.
- **Prevention:** Always monitor RSS in long-running inference services. Set memory thresholds well below the OOM kill point (Dragon has 8GB total, Ollama uses ~1.5GB, TinkerClaw ~300MB, so Dragon gets ~2-3GB headroom).

### 58. Stale session and pipeline on Tab5 reconnect (P13)
- **Date:** 2026-04-14
- **Symptom:** When Tab5 disconnects and reconnects quickly (WiFi drop, reboot), the old keepalive coroutine may still be running (sending pongs to a dead WS). Two connections exist for the same device_id in the race window before aiohttp detects the old TCP close.
- **Root Cause:** `_handle_register` did not check for existing connections with the same `device_id`. The old WS cleanup only ran when aiohttp detected the TCP close (via `async for msg in ws` loop ending), but a new connection could arrive before that detection.
- **Fix:** Added device_id collision check at the start of `_handle_register`. Iterates `_active_connections` for any existing registered connection with the same `device_id`. If found: shuts down the old pipeline, pauses the old session, marks the old connection as unregistered, and removes it from `_active_connections`. The old WS handler's `finally` block still runs but `_handle_disconnect` becomes a no-op (pipeline already None, registered=False).
- **Prevention:** Any system with reconnecting clients must handle the "new connection before old close detection" race. Always do a device_id lookup on registration, not just rely on transport-level close detection.

---

## Stability Sprint (2026-04-15)

### 59. OpenRouter API key must be in /home/radxa/.env (survives scp deploy)
- **Date:** 2026-04-15
- **Symptom:** After `scp -r dragon_voice/` deploy, cloud mode failed with "No API key configured". Had to manually restore the key every deploy.
- **Root Cause:** `scp -r dragon_voice/` overwrites `config.yaml` with the repo version which has empty API key placeholders. The live Dragon had the real key in `config.yaml`.
- **Fix:** Moved the OpenRouter API key to `/home/radxa/.env` (format: `OPENROUTER_API_KEY=sk-or-v1-...`). The systemd unit uses `EnvironmentFile=/home/radxa/.env` to load it. Config.py reads from env var when the config.yaml value is empty. `.env` is never overwritten by `scp -r dragon_voice/`.
- **Prevention:** Never store secrets in `config.yaml`. Use environment variables loaded from `/home/radxa/.env` via systemd `EnvironmentFile=`. This file survives code deploys.

### 60. Dragon shutdown: sessions.py pause_session needs try/except for db-closed
- **Date:** 2026-04-15
- **Symptom:** On Dragon shutdown, `sessions.py pause_session()` threw `sqlite3.ProgrammingError: Cannot operate on a closed database` during the cleanup of active sessions.
- **Root Cause:** During shutdown, `db.py close()` was called before all active sessions were paused. The session cleanup loop in `_handle_disconnect()` tried to update session status after the database was already closed.
- **Fix:** Wrapped `pause_session()` call in try/except for `sqlite3.ProgrammingError` and `aiosqlite.Error`. If the DB is already closed, log a warning and skip the update — the session will be cleaned up on next startup anyway.
- **Prevention:** Any database operation in shutdown/cleanup paths must handle the "already closed" case gracefully. Use try/except around DB calls in finally blocks and disconnect handlers.

### 61. Mode-aware pipeline timeouts (300s local, 60s cloud, 180s TinkerClaw)
- **Date:** 2026-04-15
- **Symptom:** Local mode with qwen3:1.7b timed out during multi-tool chains (3 tool calls = 45s+ total). Cloud mode had unnecessarily generous timeouts.
- **Root Cause:** A single 35s timeout was used for all modes. Local models are slow (7 tok/s) and tool-calling chains multiply the latency. Cloud models are fast but a 5-minute timeout masked failures.
- **Fix:** Pipeline timeouts are now mode-aware: Local (voice_mode 0) = 300s (5 min), Cloud (voice_mode 2) = 60s (1 min), TinkerClaw (voice_mode 3) = 180s (3 min, accommodates server-side tool execution gaps). Updated in `pipeline.py` swap_backends and process methods.
- **Prevention:** Always set timeouts proportional to the expected latency of each backend. Log the timeout value on mode switch for debugging.

### 62. Per-connection config deep copy prevents cross-device corruption
- **Date:** 2026-04-15
- **Symptom:** Two Tab5 devices connected simultaneously. Device A switched to cloud mode → Device B's pipeline also started using cloud backends without being told to.
- **Root Cause:** Both WebSocket connections shared the same `self._config` object (a mutable dataclass). Device A's `config_update` handler mutated the shared object, affecting Device B's pipeline.
- **Fix:** Each new WebSocket connection in `_handle_ws()` creates a `copy.deepcopy(self._config)` stored as `conn_config`. All per-connection operations (pipeline init, config_update, backend swap) use the connection-local copy.
- **Prevention:** Never share mutable config objects between connections. Always deep-copy config at connection init time. This is a classic shared-mutable-state bug — in async servers, every connection must have its own state.

---

## Rich Media Chat (2026-04-15)

### 63. TinkerClaw bypass path — media detection must be before early return
- **Date:** 2026-04-15
- **Symptom:** Rich media rendering (code blocks as images, tables as images) worked in ConvEngine mode (voice_mode 0/1/2) but produced no media events in TinkerClaw mode (voice_mode 3). Code blocks were spoken aloud as raw text instead of rendered as images.
- **Root Cause:** The TinkerClaw path in `server.py` has an early `return` after the TinkerClaw LLM response is processed (since ConvEngine, ToolRegistry, and MemoryService are all bypassed in mode 3). The media detection call (`media_pipeline.process_response()`) was placed AFTER this return, so it never executed for TinkerClaw responses.
- **Fix:** Moved media detection (`process_response()` + `strip_rendered_content()` + media WebSocket sends + `text_update`) to BEFORE the TinkerClaw early return in `server.py`, so both code paths (ConvEngine and TinkerClaw) run media detection after `llm_done`.
- **Prevention:** When adding post-LLM processing to `server.py`, always check BOTH the ConvEngine path AND the TinkerClaw bypass path. The TinkerClaw path has an early `return` — any new processing must be placed before it. Search for "tinkerclaw" and "return" in `server.py` to find the boundary.

### 64. Pygments ImageFormatter requires system fonts (DejaVu Sans Mono)
- **Date:** 2026-04-15
- **Symptom:** Code block rendering via Pygments `ImageFormatter` produced blank or garbled images on Dragon. The same code worked on the development workstation.
- **Root Cause:** Pygments `ImageFormatter` renders syntax-highlighted code as a bitmap image. It requires a monospace font to be available on the system. Dragon's minimal Debian image did not have any suitable fonts installed — Pygments silently fell back to a default that produced unreadable output.
- **Fix:** Installed `fonts-dejavu-core` on Dragon: `sudo apt install fonts-dejavu-core`. This provides DejaVu Sans Mono which Pygments uses by default.
- **Prevention:** When deploying Python packages that render text to images (Pygments, Pillow text drawing, matplotlib, etc.), always verify that the required system fonts are installed on the target machine. Add `apt install fonts-dejavu-core` to the Dragon setup script (`setup.sh`). Minimal ARM64 images rarely include fonts.

---

## Audit Wave Reconciliation (April 2026)

### 65. err_body NameError silenced the rate-limit fallback message (wave 6)
- **Date:** 2026-04-20
- **Symptom:** When OpenRouter returned a 429 or other non-200/non-400 error, Dragon's conversation turn returned an empty bubble to Tab5 — users saw nothing. Journal showed `NameError: name 'err_body' is not defined` in `openrouter_llm.py`.
- **Root Cause:** The response body was captured as `error_text = await resp.text()` on line 166 but referenced as `err_body` on line 218 inside the error-logging path. The `yield "Sorry, the cloud model had a hiccup..."` line below never executed because the NameError aborted the generator first.
- **Fix:** One-char rename — `err_body` → `error_text`. Commit `06ba2ad` on `fix/wave-6-audit`.
- **Prevention:** Always exercise the error branches of LLM backends during integration testing, not just the happy path. A rate-limited model is the easiest way to surface dead error handlers. Consider adding a fault-injection harness that swaps `resp.text()` with a mock returning a 502.

### 66. text_update must arrive BEFORE media events, not after
- **Date:** 2026-04-20
- **Symptom:** Cloud-mode responses containing code blocks rendered the raw markdown as a chat bubble ABOVE the decoded JPEG. User saw both the text and the image (duplicate content).
- **Root Cause:** Tab5's `ui_chat_update_last_message` targets the tail of the chat message store. The `text_update` message carried an empty string (the whole response was a code block that strip_rendered_content removed entirely), which Tab5 should interpret as "pop the last AI text bubble". But Dragon was sending media events FIRST (which appended an MSG_IMAGE bubble to the tail), so by the time `text_update` arrived, the "last" bubble was the image — and the empty-string pop would have removed the image, not the raw text bubble. Tab5 additionally rejected empty-string updates in the input validator, so the pop never happened at all.
- **Fix:** In `server.py` both cloud and TC paths now emit `text_update` BEFORE the media events. The guard `if cleaned != response_text` was also removed — whenever media events fire, text_update must fire (previously when the whole response was ONLY a code block, cleaned == "" != response_text was True but the check still missed the contract). Paired Tab5 fix in `ui_chat.c` accepts empty text and calls `chat_store_pop_last` to remove the raw-markdown bubble.
- **Prevention:** When replacing content via follow-up WS messages, always send the REPLACEMENT before the new content, so "replace the last element" targets the old one deterministically. Document the message order contract in `docs/protocol.md`.

### 67. Raw <tool> XML leaks into chat when MAX_TOOL_CALLS hits
- **Date:** 2026-04-20
- **Symptom:** After 3 tool calls, the LLM's final response (which might STILL contain more tool markup that qwen3:1.7b loves to emit) was yielded raw to the client, so users saw `<tool>web_search</tool><args>{...}</args>` as a chat bubble.
- **Root Cause:** In `conversation.py process_text_stream`, when `tool_calls_made >= MAX_TOOL_CALLS` OR when `has_tool_call` returned true but `parse_tool_calls` returned empty, the code fell through to the "No tool call" branch and yielded the entire buffered `full_response` via `for token in full_response: yield token`. No stripping happened at this fallthrough path.
- **Fix:** Added `_TOOL_MARKUP_RE` regex and `_strip_tool_markup()` helper. The fallthrough path now yields `_strip_tool_markup(response_text)` as a single cleaned token instead of raw tokens. Verified via WS-level probe (`tests/audit/test_d5_d6_ws.py`).
- **Prevention:** Any text that can contain control markers must be stripped at every client-facing emission boundary, not just the parsing one. Never assume "the loop will catch it" — tool-use loops have natural fallthrough cases that bypass the parse.
- **Known limitation:** qwen3:1.7b (local mode) emits iterative tool calls mid-stream. Each token is sent to the client individually before the full response is assembled for strip. A complete fix requires a server-side token buffer that holds tokens until a non-marker boundary is reached. Filed as follow-up on TinkerTab issue #78.

### 68. sqlite3 IntegrityError on register: hardware_id UNIQUE constraint
- **Date:** 2026-04-20
- **Symptom:** Synthetic WS probes connecting to Dragon with a unique `device_id` but empty `hardware_id` tripped `UNIQUE constraint failed: devices.hardware_id` on the second connection — the first probe reserved the empty string; subsequent empty-hw probes collided.
- **Root Cause:** `devices.hardware_id` has a UNIQUE constraint. Empty string counts as a value. The register handler doesn't fill in a default if the client omits it.
- **Fix:** Synthetic tests now send `hardware_id: "probe-hw-" + secrets.token_hex(6)` to dodge the constraint. Real Tab5s always send a MAC-derived ID so this doesn't hit in production.
- **Prevention:** Either make `hardware_id` default to the `device_id` if empty in `_handle_register`, or drop the UNIQUE constraint (device_id is already the PK). For synthetic/integration tests, always generate unique hardware_ids.

### 69. WiFi creds plaintext in public sdkconfig.defaults
- **Date:** 2026-04-20 (wave 13 C1)
- **Symptom:** Audit flagged real router SSID + password baked into `TinkerTab/sdkconfig.defaults` and pushed to the public GitHub repo. Password was a rotating-but-remembered secret.
- **Root Cause:** `sdkconfig.defaults` is the expected place to define Kconfig-backed strings, and the ESP-IDF build bakes them into the firmware. Someone added placeholders, someone else filled them in with live values, and the file kept getting committed.
- **Fix:** Replaced live values with `CHANGEME_SET_IN_SDKCONFIG_LOCAL` markers. Added `sdkconfig.local.example` (committed) and made `sdkconfig.local` (gitignored) the deployer's override. ESP-IDF merges `sdkconfig.local` on top of `.defaults` during reconfigure. CI plants a placeholder `sdkconfig.local` in-workflow so `idf.py build` still compiles.
- **Prevention:** Any Kconfig string that carries an environment-specific value MUST default to an obviously-invalid placeholder. If the build fails loudly when run without overrides, nobody gets tempted to "just set it here real quick". The old password is in the git history forever — rotate it on the router.

### 70. Dragon REST surface had no auth
- **Date:** 2026-04-20 (wave 13 C2)
- **Symptom:** `/api/v1/sessions`, `/api/v1/memory`, etc. were reachable from any host on the LAN (and via ngrok from anywhere) with zero credentials. Anyone could read conversation history or inject fake sessions.
- **Root Cause:** No middleware gated the REST routes. The WS handshake authenticated at the `register` frame, but HTTP did not.
- **Fix:** New `_auth_middleware` in `server.py` + `ServerConfig.api_token` field + `DRAGON_API_TOKEN` env override. All routes require `Authorization: Bearer <token>` except the `_AUTH_PUBLIC_PREFIXES` allowlist (`/health`, `/ws/voice` handshake, `/dashboard`, `/api/media/`). Constant-time comparison via `hmac.compare_digest`. Fail-closed with HTTP 503 when token is unconfigured. Regression tests in `tests/test_auth_middleware.py` (6 cases).
- **Prevention:** Every new HTTP route handler must either land under an existing public prefix or add itself to the allowlist explicitly. The middleware's fail-closed behaviour (503 on unconfigured) makes "forgot to set the token" impossible to miss.

### 71. TinkerClaw gateway token silently sent as blank
- **Date:** 2026-04-20 (wave 13 H6/H7)
- **Symptom:** Voice mode 3 (TinkerClaw) returned 401s on every request when the config tree was missing the gateway token, but the error path surfaced only a generic "LLM unavailable" to the user.
- **Root Cause:** `TinkerClawBackend.__init__` copied `config.tinkerclaw_token` without validation. A blank string was a valid Python value that silently produced a broken `Authorization: Bearer ` header.
- **Fix:** Constructor now raises `ValueError` with actionable message if token resolves to blank. `TINKERCLAW_TOKEN` env override lives in `config.load_config` (same pattern as `DRAGON_API_TOKEN`). `config.yaml` ships with the placeholder blank and a comment pointing at `/home/radxa/.env`.
- **Prevention:** Never accept a blank credential as valid config at construction time. Validate at the boundary closest to the caller so the error message carries the most context. The pattern to copy: read → strip → raise-if-blank.

### 72. pipeline.py silent swallow masked WS teardown errors
- **Date:** 2026-04-20 (wave 13 H5)
- **Symptom:** Clients disconnecting mid-turn produced cascading "Processing failed" log spam because the secondary `_on_event({"type":"error"...})` emit would raise a wholly different `ConnectionResetError`, get logged at `exception` level by the outer `logger.exception`, and mask the original pipeline fault.
- **Root Cause:** Three `except Exception: pass` handlers in `pipeline.py` silently swallowed all exceptions — when the inner emit failed, the primary error was still logged, but the silent pass hid a signal we actually want.
- **Fix:** Narrowed the three silent swallowers to `(ConnectionError, RuntimeError[, AttributeError])` and replaced `pass` with a `logger.debug(...)` that names the handler. The primary `logger.exception` paths keep `except Exception` because they surface the error for diagnosis.
- **Prevention:** If a handler is silently swallowing, narrow the catch until the specific class makes sense. `except Exception: pass` is a smell — it means the author didn't know what can fail. The acceptable version is a narrow catch of expected failure modes plus at least a `logger.debug`.

### 73. ClientSession on module-global bound to wrong event loop
- **Date:** 2026-04-20 (wave 13 H4)
- **Symptom:** Test harness that restarted the dashboard app between tests raised `RuntimeError: Session is closed` or "Timeout context manager should be used inside a task" depending on the test ordering. Production dashboard didn't show this — only the test rig did.
- **Root Cause:** `_client: aiohttp.ClientSession | None = None` was a module global initialized in `on_startup`. On app teardown it was set to None, but the second app's `on_startup` created a new session bound to a *new* event loop — yet helpers accessing `_client` could still see a stale reference in flight.
- **Fix:** Moved the session onto `app[CLIENT_KEY]` (aiohttp's `AppKey` API). Helpers now take the `app` explicitly (`_fetch_json(app, url)`). Lifecycle is app-scoped; each app owns its own session.
- **Prevention:** Never hold an aiohttp session in a module global. Use `web.AppKey` + `app[key]`. This is the pattern aiohttp 3.9+ actively recommends.

### 74. media_cleanup task not cancelled on shutdown
- **Date:** 2026-04-20 (wave 13 H3)
- **Symptom:** Graceful shutdown logged "Task was destroyed but it is pending" for `_media_cleanup_loop` when systemd sent SIGTERM during the 24h TTL sleep.
- **Root Cause:** `_on_shutdown` cancelled `_memory_monitor_task` and `_purge_task` but missed `_media_cleanup_task` entirely. The loop sat in `asyncio.sleep(3600)` and the event loop closed out from under it.
- **Fix:** Added cancel + await-with-suppressed-CancelledError for `_media_cleanup_task` in `_on_shutdown`, matching the sibling cleanup tasks.
- **Prevention:** Every `asyncio.create_task(...)` that lives past the request must be tracked in a single `self._*_task` field AND cancelled in `_on_shutdown`. Grep for `create_task` before adding a new long-lived task.


### 77. Yahoo Finance 429s Dragon's static IP — Stooq CSV is the usable free quote feed
- **Date:** 2026-04-23 (#60)
- **Symptom:** `stock_ticker` tool's first implementation used Yahoo Finance chart API (`query1.finance.yahoo.com/v8/finance/chart/{symbol}`). Second call onwards returned HTTP 429 "Too Many Requests" from Dragon's IP — even with a spoofed desktop User-Agent. Yahoo blocks datacenter IPs aggressively.
- **Root Cause:** Yahoo's chart API is rate-limited by IP, not by key or UA. Dragon's static ngrok-adjacent IP gets flagged quickly.
- **Fix:** Made Stooq CSV the primary source (`https://stooq.com/q/l/?s={sym}.us&f=sd2t2ohlcv&h&e=csv`, no auth, stable on Dragon), Yahoo Finance the fallback for when Stooq is unreachable. Tool returns the source name in its dict so debug logs show which fed the answer.
- **Prevention:** For any free public API that might rate-limit, design with two sources — primary + fallback — from the first commit. Log the source in the result so regressions are visible from a single turn.

### 78. Local-mode LLM: Dragon's 30 s WS keepalive is a hard model-size ceiling
- **Date:** 2026-04-24 (TinkerTab audit + 11-model re-benchmark)
- **Symptom:** Every 4B-class Ollama model on Dragon (qwen3:4b, qwen3.5:4b, nemotron-3-nano:4b) produced **empty replies in Tab5 chat** even though the Ollama stream eventually completed on Dragon.  A Hello prompt to `qwen3:4b` took 111 s end-to-end; Tab5's chat bubble stayed blank.  Meanwhile CLAUDE.md's benchmark table called 4b "Excellent 97.5%" tool calling — great accuracy numbers, useless in practice.
- **Root Cause:** Tab5's voice WS library times out after 30 s with no PONG and triggers a reconnect.  The reconnect hits `server.py`'s P13 "Device already has connection" guard, which evicts the old ws and cancels the in-flight LLM stream (`Pipeline processing cancelled`).  The response finishes generating on a dead connection and gets thrown away.  Any model whose end-to-end latency exceeds ~30 s is architecturally dead in Local mode, independent of how smart it is.
- **Fix:** Switched default `dragon_voice/config.yaml` `ollama_model` from `qwen3:0.6b` to `ministral-3:3b`.  Out of 11 sub-4B models gauntleted (see CLAUDE.md "Local LLM Benchmarks"), ministral-3 was the only one that (a) returned within the keepalive window, (b) actually fired tools (3/5 on the gauntlet, including correct arithmetic on 456×789 = 359,784 — a value chosen specifically because it's not in any training corpus), and (c) stored a memory fact without leaking prior-session context.
- **Prevention:**
  1. **Never benchmark tool-calling accuracy without measuring end-to-end latency against the WS keepalive ceiling.**  A model that hits 97 % accuracy at 90 s per turn is 0 % useful in Local voice mode.
  2. **Pick non-memorized test inputs.**  The prior qwen3:0.6b audit passed on "12 + 7" because the model answered from weights.  The re-benchmark switched to `456 × 789` and three models that "passed" old tests returned wrong numbers (356,664 / 356,184 / 359,964).
  3. **Cross-check tool claims against the memory DB.**  Several models say "I've stored that for you" but `GET /api/v1/memory` shows zero rows.  Verbal-ack ≠ tool-fire.
  4. **Three failure classes to screen against:** *too small* (qwen3 0.6b/1.7b — malformed XML), *too slow* (all 4B models), *fluent hallucinator* (llama3.2:3b, hermes3:3b, phi4-mini — confident chatty answers with zero tool fires).
- **See also:** `docs/historical/AUDIT-WAVE-14.md` → "Local-mode model gauntlet 2026-04-24" section.  W15-OPS-2 in `docs/WAVE-15-PROGRESS.md` for the config-change audit trail.  The P13-eviction fix and `aut_tier` wiring are tracked separately — neither is a model-choice problem.

### 79. Tool-call dialect fragmentation: parser was fighting the training prior
- **Date:** 2026-04-24 (TinkerTab audit round 2/3, same day as #78)
- **Symptom:** Every purpose-built function-calling fine-tune pulled from HuggingFace (Salesforce/xLAM-2-1b-fc-r, NovachronoAI/LFM2.5-1.2B-Nova-FC, contextboxai/Qwen3-1.7B-FC, distil-labs/distil-home-assistant-functiongemma) registered `tools=0` on every prompt in the Local-mode gauntlet, even though the model output clearly contained a tool call.  xLAM would emit `[tool>calculator</tool><args>{"expression": "456 * 789"}</args>`, LFM2.5-Nova would emit `<tool_call>{"name": "calculator", "arguments": {...}}</tool_call>`, and both landed in the chat bubble as inert-looking text.
- **Root Cause:** TinkerBox's tool-call parser (`dragon_voice/tools/registry.py`) was anchored on a single legacy XML dialect — `<tool>NAME</tool><args>{json}</args>` — which is what Dragon's own system prompt asks models to emit.  Every function-calling fine-tune on HF is trained to emit the industry-standard `<tool_call>{"name": "X", "arguments": {...}}</tool_call>` regardless of what system prompt it sees, because that's what its SFT data was shaped like.  Dragon told models to use dialect A; the models answered in dialect B; the parser only knew dialect A; nothing fired.  Additionally, the xLAM quantized GGUF emits the opening tag as `[tool>` / `<tool]` / `[tool]` with stray brackets — the closing `</tool>` is always intact but the old strict-`<tool>` regex dropped the whole match.
- **Fix:** `dragon_voice/tools/registry.py` (PR #74) now runs two anchor passes.  Legacy pass: open-tag character class widened to `[<\[]tool[>\]]` so all four bracket permutations match; otherwise identical to the prior JSON-walker logic.  Standard pass: new anchor for `<tool_call>\s*{json}</tool_call>`, extracts the `name` + `arguments` (or `args`) keys, emits the same `{"tool": ..., "args": ...}` record the executor expects.  Both dialects feed the same downstream `execute()` path and WS eventing, so the change is surface-only.  11 unit tests cover both dialects, all four bracket quirks, mixed chains of both dialects in one reply, and the "bare `<tool>` with no args" Nova quirk.  After deploy, xLAM-2-1b-fc-r's tool-fire count jumped from 2/5 to 4/5 with one real memory-DB write; no regression on ministral-3:3b (still 2/5, same memory write path).
- **Prevention:**
  1. **When adopting a purpose-built tool-calling fine-tune, accept its native dialect rather than fight its training prior.**  Parsers are cheap; retraining is not.
  2. **Any tool-format change has to be a UNION, not a swap.**  Dialect A models still exist (ministral-3 emits A correctly); legacy must keep working.
  3. **Don't support open-ended tag-name dialects** (LFM2.5-Nova's `<tool_X>{args}</tool_X>` style) — tool names collide with the tag grammar and the disambiguation gets ugly fast.  Better fix is a per-model Jinja template that coerces the output toward a supported shape.
- **See also:** `docs/historical/AUDIT-WAVE-14.md` → "Local-mode gauntlet Round 2 + 3".  The "xLAM + responder" dual-model pipeline is tracked as a future proposal — same parser change unblocks it without further code.

### 80. Dual-model local pipeline doesn't fit Dragon's RAM ceiling
- **Date:** 2026-04-25 (PR #80, dual-model pipeline implementation + bench)
- **Symptom:** Implemented a transparent `DualModelBackend` that pairs a fast tool-picker (xLAM-2-1b-fc-r, 1.1 GB) with a warm responder (ministral-3:3b, 4.5 GB).  Single-prompt smoke tests worked end-to-end on Dragon (datetime → tool fired → ministral wrote a real reply in 26 s).  But the 10-prompt sandbox gauntlet collapsed after 2 turns: G1 picker-text fallback OK, G2 calculator → 359,784 correct (261 s), G3-G10 all `[Ollama timeout after 300 s]`.  Switching the responder to qwen3:1.7b (1.4 GB) didn't help — qwen3:1.7b returns *empty* `message.content` from raw Ollama on Dragon ARM64 even with `num_predict=50` (independent bug).
- **Root Cause:** Dragon Q6A reports 11 GB total RAM but the steady-state working set (Dragon services + Python process + STT/TTS + embeddings) holds ~6 GB resident before any LLM loads.  xLAM (1.1 GB) + ministral (4.5 GB) = 5.6 GB of model weights.  Total resident hits ~8.7 GB out of 11 GB.  Ollama's eviction kicks in *despite* `keep_alive=5m` because RAM pressure outranks the keep-alive hint — the LRU model gets dropped, the next turn's first call pays a ~30 s disk reload, the OTHER model becomes LRU, and the pair ping-pongs every turn.  After 2-3 turns Ollama serializes itself badly enough that every subsequent generation hits the 300 s aiohttp `total` timeout in `OllamaBackend`.  Documented plan estimate (4.7 GB combined) was off — ministral resident is 4.5 GB, not the 2.8 GB on-disk size.
- **Fix:** Shipped dual.py opt-in; default `backend` stays `ollama` + `ministral-3:3b` for Dragon.  Two stacked bugs were found and fixed during the bench (independently valuable): (a) `server.py` unconditionally reset `conn_config.llm.backend = local_backend or "ollama"` on every WS connect, silently flipping any non-ollama local backend back — gated to `backend ∈ {openrouter, tinkerclaw}` so cloud → local fallback still works; (b) `pipeline.py:_llm_sig` returned `("llm", "dual", "")` for every dual config, so two different dual setups would collide on the same pool key — extended the signature to include picker+responder model names.  Plumbed `LLMConfig.ollama_keep_alive` so dual sub-configs can opt into "5m" without changing the default 30 s for solo users.
- **Prevention:**
  1. **For multi-model pipelines, measure RAM with `free -h` *during* a sustained gauntlet, not by summing on-disk model sizes.**  The on-disk Q4_K_M of a 3 B model is 1.9 GB, but Ollama's runtime working set (KV cache, attention scratch, layout overhead) is 2-2.5 × that.
  2. **`keep_alive` is a hint, not a guarantee.**  Under RAM pressure Ollama evicts the LRU regardless.  If the architecture *requires* both models warm, the fix is shrinking models or growing RAM, not tuning the hint.
  3. **Single-prompt smoke tests are insufficient validation for memory-pressure code paths.**  The first prompt always works because RAM is fresh.  Run ≥ 5 back-to-back turns to surface eviction thrash before declaring victory.
  4. **When validation fails on the target hardware but the code is sound, ship opt-in with the constraint documented.**  The dual code is correct architecture for ≥ 16 GB targets and the fixes it required (server reset gating, pool-key dual-awareness) are universally valuable.  Pretending the architecture is broken would lose the latter; pretending the bench succeeded would lose user trust.
- **See also:** `docs/PLAN-dual-model-pipeline.md` → "Validation results" section for the per-prompt matrix.  Three follow-up issues to file separately: (1) widen parser for the 4th xLAM dialect `[NAME]ARGS()` / `[NAME]{json}</NAME>`, (2) re-bench dual on ≥ 16 GB target, (3) investigate the qwen3:1.7b empty-reply bug on Dragon.

### 81. Cancel cmd skipped pipeline.cancel when text task was cancelled (audit A1)
- **Date:** 2026-04-26 (PR for #138)
- **Symptom:** Cancelling a text turn while Piper TTS was synthesizing left the Piper subprocess alive until natural completion, holding CPU + an FD.  Two cancels in a row stacked two zombies.  `cancel_ack.cancelled = ['text']` instead of the expected `['text', 'pipeline']`.
- **Root Cause:** The cancel handler in `server.py` gated `pipeline.cancel()` on `pipeline._processing or not cancelled_what`.  In the text-cancel case `pipeline._processing` was False (it's the voice-pipeline-utterance flag, not text) and `cancelled_what` was non-empty (just appended `'text'`), so the gate evaluated False and `pipeline.cancel()` was skipped.  But the text path in `_handle_text` calls `pipeline._tts.synthesize(response_text)` directly, spawning Piper subprocesses tracked in `PiperTTS._active_procs`; only `pipeline.cancel()` calls `tts.kill_active_procs()`.  Net effect: voice-path cancel killed Piper correctly, text-path cancel did not.
- **Fix:** Drop the gate.  `pipeline.cancel()` is idempotent (it sets `_cancelled`, kills procs from a list that's safe to walk when empty, clears empty buffers).  Always call it on cancel.  Verified live on sandbox :3513 with side-by-side OLD-vs-NEW runs of `/tmp/cancel_kills_piper_test.py`: OLD returned `cancelled=['text']`, NEW returned `cancelled=['text', 'pipeline']`.
- **Prevention:** When a "skip if X" optimisation predates a code path that violates X's premise (here: text-path TTS bypassing `pipeline.start_processing()`), the optimisation becomes a silent bug.  For cleanup paths (cancel, shutdown, disconnect), prefer "always run, idempotent" over "skip if not needed" — the skip saves microseconds and costs subprocess leaks.  Audit pattern: any condition of the form `if X._processing or not Y` near a cleanup call should justify why the gate is sound rather than just defensive.

### 82. Backend swap silently half-applied when ConvEngine LLM init raised (audit A4 + B7)
- **Date:** 2026-04-26 (PR for #140)
- **Symptom:** `config_update voice_mode=3` with a blank `tinkerclaw_token` was supposed to be rejected with a user-visible error.  Pre-fix: pipeline swap succeeded (it only owns STT/TTS), then ConvEngine LLM swap raised `ValueError("tinkerclaw_token is blank ...")` which was caught with a bare `logger.exception` (server.py:2147-2148) — no Tab5 frame.  Tab5 received a green `config_update` confirming `voice_mode=3` and put the user in TC mode while ConvEngine still held the previous Ollama LLM.  Next text/voice turn answered from Ollama with no indication.  Even when the failure path *did* fire (the top-level `except Exception` for pipeline.swap_backends), it sent `f"Backend swap failed: {e}"` directly into Tab5's voice caption — a multi-line raw Python message.
- **Root Cause:** Two separate gaps.  (B7) `_handle_config_update` validated `openrouter_api_key` for cloud modes but had no equivalent check for `tinkerclaw_token` in mode 3, despite the TC backend's `__init__` raising on blank.  (A4) The `except Exception` for the swap interpolated `str(e)` into the user-facing `config_update.error` field — the `error_event(...)` γ-arch helper (Phase 3 γ1) had been built for exactly this but the swap site was never migrated.  And the second swap (ConvEngine LLM) had no Tab5-visible error path at all.
- **Fix:** Added B7 pre-flight: blank-token check for mode 3 alongside the existing OpenRouter key check, emitting `error_event(code="tc_token_missing", severity=FATAL, scope=GATEWAY)` + a separate config_update revert.  Migrated the A4 swap-failure site to emit a γ-arch event with a generic user-facing message ("Couldn't switch backends — reverted to local"); the raw exception still goes to logs via `logger.exception`.  Added a `DragonError` branch so structured errors thrown by backends are forwarded verbatim instead of getting flattened.  Verified live on sandbox :3513 with side-by-side OLD vs NEW: OLD = silent half-swap (no error, no revert), NEW = clean error + revert.
- **Prevention:**
  1. **Every config-changing path needs a Tab5-visible error frame for every failure mode.**  A `logger.exception` without a paired emit is a Tab5-invisible bug.
  2. **When introducing a structured-error helper (γ-arch), add a CI check or migration ticket for ALL existing ad-hoc emission sites.**  Half-migrated channels are worse than none — they hide which sites still leak.
  3. **For multi-step swaps (pipeline + ConvEngine), validate every required input up-front.**  A blank token caught at step 1 is one error; a blank token caught at step 2 leaves step-1's state already mutated.

### 83. Voice path silently dropped tool callbacks; voice was three audit gaps behind text (audit A2 + A3 + C5)
- **Date:** 2026-04-26 (PR for #142)
- **Symptom:** Tab5 chat showed tool indicators on text turns but never on voice turns: voice "weather in Tokyo" fired `web_search` server-side, the result was ranked into the LLM's reply, but the chat bubble never rendered the `tool_call` / `tool_result` chip.  Voice "remember magenta" with FC-style empty NL output got the legacy "Sorry, I couldn't generate a response" instead of "Got it — magenta."  Same prompts typed worked correctly.
- **Root Cause:** The voice path in `pipeline._process_utterance` had not been updated when the text path got the #75-family (tool-callback plumbing, per-tool wrap, `looks_like_useful_text` heuristic).  Three gaps had accumulated: (A2) `process_text_stream(...)` was called without the `on_tool_*` kwargs so ConversationEngine's tool events evaporated; (A3) the empty-reply guard hard-coded the generic apology with no per-tool wrap branch; (C5) the empty-reply check was a strict `not full_response.strip()` rather than the smarter `looks_like_useful_text` heuristic, so bracket-noise-only replies (xLAM residual `<`, gemma3:4b stray `<`) silently slipped past the guard and rendered as empty bubbles.
- **Fix:** Moved `looks_like_useful_text` from `server.py` to `tools/response_wrap.py` (single source of truth for both paths).  Added optional `on_tool_call`/`on_tool_result`/`on_tool_error` kwargs to `VoicePipeline.__init__` plus a per-utterance `_tool_calls_this_turn` tracker.  Server's `_handle_register` now passes the same `conn_state["on_tool_*"]` callbacks it already gives the text path.  In `_process_utterance` the wrapped callbacks populate the tracker AND forward to the user-supplied callback; end-of-stream uses the same `looks_like_useful_text` + `synthesize_wrap` shape as text.  19 unit tests pin the heuristic; 5 mocked-engine tests pin the callback plumbing + wrap selection (verified pre-fix: 0 of 5 pass — `TypeError: VoicePipeline.__init__() got an unexpected keyword argument 'on_tool_call'`).
- **Prevention:**
  1. **Voice and text paths must share post-process logic, not duplicate it.**  Three audit gaps in one diff is the failure mode of "fix the obvious site, ship, forget the parallel one."  When a behavioural fix lands on one path, file a follow-up issue for the other path the SAME day even if the fix doesn't trivially port.
  2. **A "private helper" in server.py that ought to be shared is the smell.**  `_looks_like_useful_text` was a leading-underscore module-private; the leading underscore signalled "internal" but the actual scope was "all post-process call sites in this app."  When a private helper grows two callers in different modules, promote it before adding a third.
  3. **For optional callback kwargs, prefer `Optional[Callable]` with `None` default over "shrug, don't pass it."**  The voice path silently omitted `on_tool_*` because the kwargs weren't on the signature *or* in the docstring.  Once they were optional on the receiving side (ConversationEngine had them since #75), the omission became invisible.  Adding them as documented optional kwargs on the *caller* (VoicePipeline) is the cheap forcing function.

### 84. TC mode buffered the entire reply — 30-60 s "thinking" before any token (audit A5)
- **Date:** 2026-04-26 (PR for #144)
- **Symptom:** TinkerClaw mode (voice_mode=3) showed the "thinking" indicator for 30-60 s, then the entire reply landed at once.  Tab5 chat felt broken: no streaming token-flow even though the TC SSE stream was sending content deltas the whole time.
- **Root Cause:** The 2026-04-24 #B1 audit fix in `tinkerclaw_llm.py:300-308` added `accumulated: list[str] = []` and yielded only at end-of-stream so `sanitize_tinkerclaw_reply` could peel CoT preamble that TC agents leak between tool calls ("Let me try another approach: ...").  Correct correctness, terrible UX.  The sanitiser's patterns are all `^`-anchored — they only ever peel from the FRONT of the response — so buffering the whole reply is overkill; only the prefix needs buffering.
- **Fix:** Two-phase streaming.  Buffer until one of (a) `len >= 400` (safety cap), (b) a `\n\n` paragraph break is seen (TC agents put a blank line before the final answer), or (c) a sentence-end punctuation arrives past `len >= 60` AND the sanitised residue carries `len(strip()) >= 20` chars of real content (the answer has started).  At any of those triggers, yield the cleaned prefix and switch to passthrough mode where every subsequent token streams directly.  Cancel/abort/interrupted paths preserve today's "flush sanitised buffer" behaviour.
- **Prevention:**
  1. **Correctness fixes that hold UX hostage need a follow-up issue filed the same day.**  The #B1 fix solved a real problem (preamble leaking into the chat bubble) but introduced a bigger UX regression that took two months and a stress-test audit to surface.  When you trade latency for correctness, file the "now make it streaming-friendly" follow-up at commit time.
  2. **Pattern-anchored regexes are a hint about how much buffering is needed.**  Every `_COT_PREAMBLE_PATTERN` is `^`-anchored — ergo only the front needs buffering.  Read the regexes before designing the buffer.
  3. **Test the user-perceived stream shape, not just the joined output.**  Unit tests must assert `len(out) >= 2` (multiple yielded chunks) for streaming-relevant behaviours; pre-fix tests only checked `"".join(out)` content, which masked the buffer-then-yield-once behaviour.

### 90. Scheduler-fired widgets interleaved with LLM tokens; cancel didn't reach them (audit B1)
- **Date:** 2026-04-26 (PR for #165)
- **Symptom:** Scheduler reminders firing while a user's voice/text turn was streaming caused the WS frame stream to interleave: `llm "hello" → widget_card "Reminder!" → llm "world"`.  Tab5 chat rendered the widget mid-bubble and the user lost their place.  Worse: when the user tapped stop mid-turn, the WS cancel killed the LLM stream + Piper procs but a queued reminder fire still emitted its widget — causal-link broken.
- **Root Cause:** `scheduler._fire_one` called `surface.card(...)` directly with no awareness of in-flight turns.  Pipeline / ConvEngine had no shared "turn-busy" signal that out-of-band emitters could check.  The cancel handler had no hook into the scheduler-fire path.
- **Fix:** Added a per-session **TurnGate** to `SurfaceManager`: `mark_turn_start(sid)` / `mark_turn_end(sid)` (drains deferred), `defer_or_send(sid, send_fn)` (queue if busy, send if idle), `discard_deferred(sid)` (cancel hook).  Wired `pipeline._process_utterance` + `server._handle_text` to bracket their bodies with mark_start/end via try/finally so even raised exceptions cleanly drain.  WS cancel handler calls `discard_deferred` and reports the drop count in `cancel_ack.cancelled` (e.g. `["text", "pipeline", "deferred:1"]`).  `scheduler._fire_one` routes its live-card emit through `defer_or_send` instead of calling `surface.card` directly.  Tool-call widgets (TimeSense, QuickPoll) deliberately bypass the gate — they're synchronous to the turn and must land immediately.  14-test TurnGate unit suite + 50 existing scheduler tests still green.
- **Prevention:**
  1. **Out-of-band emitters need a coordination contract with the in-flight pipeline.**  Any future async-fire skill (notifications, alarms, progressive summaries) should consult the same TurnGate API rather than racing the LLM stream.
  2. **Queue + drain on a clean session-boundary hook beats interleaving + reordering on the receiver.**  Tab5 cannot reliably re-order `widget_card` against `llm` token frames; the right answer is to defer at the source.
  3. **Cancel must touch every concurrent emitter for a turn, not just the LLM.**  The audit caught this exactly because the cancel handler was tightly coupled to pipeline/handler-task slots and had no awareness of out-of-band sources.

### 85. OpenRouter TTS swallowed every failure as `b""` — pipeline fallback never fired (audit B3)
- **Date:** 2026-04-26 (PR for #146)
- **Symptom:** Cloud/Hybrid voice modes with a broken OpenRouter key (or empty SSE response, or network blip) showed the text reply but produced no audio.  No fallback to local Piper, no error toast on Tab5.  The pipeline's `except (Exception, asyncio.TimeoutError)` fallback path at `pipeline.py:1224` exists and works correctly — but never fires for the OpenRouter TTS failure modes because the backend swallows them all as `b""` instead of raising.
- **Root Cause:** `OpenRouterTTSBackend.synthesize` had three failure-then-`return b""` paths (HTTP non-200, no audio chunks in SSE, generic exception).  Caller's `if audio_bytes:` guard then silently skipped synthesis with no signal that anything was wrong.  Same Tab5-invisible failure class as #82's `logger.exception`-only path.
- **Fix:** Replace each `return b""` with `raise DragonError(severity=TRANSIENT, scope=TTS, code=...)`.  `DragonError` is a subclass of `Exception` so the existing pipeline fallback triggers automatically — local Piper takes over and the existing `config_update {voice_mode: 0}` revert frame surfaces on Tab5.  Backend's catch-all `except Exception` re-raises as a structured DragonError with the original cause attached.  4 unit tests pin the new behaviour (HTTP error, empty SSE, network exception, happy path); pre-fix verified failing 3/4.
- **Prevention:**
  1. **A "return empty value" is not error handling — it's ostrich code.**  Every "return b''/None/[]" in a backend method is a place where the caller can't tell success from failure.  When the next layer's logic depends on the difference, that backend MUST raise.
  2. **Audit pattern: grep for `return b""` / `return None` / `return []` in every backend (STT/TTS/LLM/embedding).**  Each one is a candidate for the same B3-class bug.  If the caller has a fallback path, the silent return is bypassing it.
  3. **Use the existing `DragonError(cause=...)` form.**  The `cause=` kwarg + `from e` raise preserves the original traceback for ops debugging while shipping a user-facing message that doesn't leak implementation detail.

### 86. MAX_TOOL_CALLS=3 hit silently — reply read as truncated mid-thought (audit B4)
- **Date:** 2026-04-26 (PR for #148)
- **Symptom:** When an LLM emitted a 4th tool call after `MAX_TOOL_CALLS=3` was already hit, ConversationEngine silently fell through to the strip-markup branch.  User saw a reply that read as cut off mid-thought ("Let me check the calendar…") with no signal that the chain stopped.
- **Root Cause:** `conversation.py:393` gates tool execution on `has_tool_call(...) AND tool_calls_made < MAX_TOOL_CALLS`.  When the second clause fails, the loop just exits without firing any callback, log, or Tab5 frame.  Same Tab5-invisible failure-mode class as #82 (silent half-swap) and #85 (silent `b""`) — a "guard returns false" path that never communicates the guard fired.
- **Fix:** Add a separate detection branch *before* the execution gate: when `has_tool_call(...) AND tool_calls_made >= MAX_TOOL_CALLS`, fire `on_tool_error` with `code="tool_call_limit_reached"`, `limit=MAX_TOOL_CALLS`, and a user-facing message naming the limit.  Server's `_on_tool_error` callback (originally only for parser failures) now honours the err dict's `code` and `message` instead of hardcoding `tool_args_invalid`, so the new code reaches Tab5 verbatim.  3 unit tests pin the new behaviour; pre-fix verified failing on the limit-trigger test.
- **Prevention:**
  1. **Every "if X and Y" gate where Y can flip false at runtime is a candidate for a "Y was the false clause" signal.**  Audit pattern: search for `< MAX_`, `< _MAX_`, `< limit`, etc., and check whether the false case is observable to the user.
  2. **A callback that handles "thing went wrong, here's a code+message" should not hardcode the code on the receiving side.**  The receiver should respect the sender's `code`/`message` fields and only fall back to its own defaults when the sender omitted them.

### 87. PCM resample duplicated voice-vs-text TTS — extracted to dragon_voice.audio (audit B8)
- **Date:** 2026-04-26 (PR for #150)
- **Symptom:** Quality work on the resample (e.g. swapping linear interp for a polyphase filter) would silently miss the parallel callsite.  Same divergence shape as #83 (voice-vs-text post-process).
- **Root Cause:** Two near-identical 10-line linear-interp resample blocks lived inline in `pipeline._synthesize_and_send` and `server._handle_text`.  No shared helper.
- **Fix:** Extracted to `dragon_voice/audio.py:resample_pcm16(audio_bytes, src_rate, dst_rate)`.  Same-rate fast path short-circuits with no allocation.  Empty input + 1-sample edge cases handled.  8-case unit test pins behaviour, including a byte-identical match to the pre-extraction formula so any future polyphase swap requires an explicit test update.
- **Prevention:** Use the file-split smell test from CLAUDE.md ("what stakeholder cares about the code I'm moving?") in reverse — when the *same* code lives in two files maintained by the *same* concern (audio plumbing), that's the smell that the helper's natural home doesn't exist yet.  Build it before the third copy arrives.

### 88. Dictation post-process cancellation didn't await — race-emit possible (audit B5)
- **Date:** 2026-04-26 (PR for #152)
- **Symptom:** Rapid stop+restart of dictation could double-emit `dictation_summary` (old transcript + new) or silently lose the summary on a disconnect-mid-LLM.  Both `pipeline.cancel()` and `pipeline.finish_dictation()` called `self._post_process_task.cancel()` but never awaited the task.
- **Root Cause:** `task.cancel()` is non-blocking — it just schedules a CancelledError to fire at the task's next await point.  Pre-fix the caller (`cancel()` / `finish_dictation()`) returned immediately after `task.cancel()`, so `_post_process_task = None` happened in parallel with the still-running task — the new `finish_dictation` could spawn task B while task A was still running, and any non-await section in A (e.g. between LLM-stream end and emit start) couldn't be interrupted.
- **Fix:** `prev.cancel()` followed by `try: await prev / except (CancelledError, Exception): pass` in both sites.  By the time the caller continues, the previous task is guaranteed CANCELLED or completed cleanly.  4-test suite pins the new contract; the most strict test uses `asyncio.shield()` around a synthetic post-process body to force the race deterministically — pre-fix verified failing with the assertion `cancel() returned at X BEFORE task completed at Y — cancel() didn't await`.
- **Prevention:**
  1. **`task.cancel()` without a paired `await task` is half a cancel.**  The caller has no idea when the cancellation actually completed.  Audit pattern: grep for `\.cancel\(\)$` lines that aren't immediately followed by `await` or aren't inside a `gather(..., return_exceptions=True)`.
  2. **Use `asyncio.shield()` in a regression test to deterministically reproduce a fast-completing-post-cancel race.**  Real-world LLM streams cancel cleanly because every `await` checks for cancellation; a shield is the cheapest way to mimic "this section can't honor cancellation right now" in a unit test.

### 89. Hybrid first-utterance after blip ate STT timeout + Moonshine cold-load (audit B6)
- **Date:** 2026-04-26 (PR for #154)
- **Symptom:** First Hybrid-mode utterance after a network blip paid 15 s of cloud-STT wait_for + 1-3 s of Moonshine cold-load + 1 s of local transcribe = 17-19 s before any user signal.  Tab5 showed the spinner in silence, users assumed the device had hung.
- **Root Cause:** Three independent latencies stacked end-to-end: (a) `asyncio.wait_for(timeout=15)` budget on the cloud STT call — too generous for first-blip recovery; (b) Moonshine fallback was lazy-initialised on first failure, so the cold-load ran on the synchronous critical path; (c) no progress event was emitted between the cloud STT failure and the local transcribe completing, so the user saw 1-4 s of total silence.
- **Fix:** Three small changes in one PR.  (a) Tightened `wait_for` from 15 s → 10 s — past 10 s the user has already perceived "broken", fast-fail beats waiting.  (b) `swap_backends` now schedules a background Moonshine prewarm whenever the new STT backend is `openrouter`; idempotent so concurrent swaps don't queue duplicate tasks; cancelled cleanly on `shutdown()`.  (c) Fallback path emits a γ-arch `stt_fallback_active` event before transcribing locally, so Tab5 can show "Cloud STT slow — switching to local."  4 unit tests pin the prewarm spawn / idempotency / in-flight reuse / timeout value.  Pre-fix verified 4/4 failing.
- **Prevention:**
  1. **`asyncio.wait_for` timeouts are user-perception budgets — calibrate them to "the user has already given up" not "the network might recover".**  15 s in a real-time voice pipeline is forever.
  2. **Lazy-init on the critical path is the same anti-pattern as cold caches in HTTP servers.**  If a fallback exists, prewarm it the moment the primary becomes "this might fail" — for STT, that's the swap into a cloud backend.
  3. **Every multi-second wait in a synchronous user path needs a paired progress emit.**  "No signal" is worse UX than "negative signal" — Tab5 can render a transient toast for "Cloud STT slow", but it can't render anything from silence.

### 90. OpenAI multimodal `content` array sent to Ollama hits a 400 unmarshal error
- **Date:** 2026-04-27
- **Symptom:** First end-to-end test of the multi-model router with MiniCPM-V on Dragon: router correctly picked the vision model, lazy-instantiated the backend, but every vision turn returned `[Ollama error: 400]`. Server log: `Ollama error 400: {"error":"json: cannot unmarshal array into Go struct field ChatRequest.messages.content of type string"}`.
- **Root Cause:** The `messages` list our caller sends is OpenAI multimodal format — `{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:..."}},{"type":"text","text":"..."}]}` (a content *array* of typed parts).  Ollama's `/api/chat` expects a flat `content` *string* + a sibling `images: [base64]` array (raw bytes, no `data:` prefix).  Three other backends (openrouter, lmstudio, tinkerclaw) all natively accept the OpenAI shape; OllamaBackend was passing the multimodal messages straight through to Ollama, which JSON-rejected them.
- **Fix:** Added `_translate_to_ollama_format(message)` in `dragon_voice/llm/ollama_llm.py` that walks any list-typed `content`, extracts `image_url` URLs (strips the `data:image/jpeg;base64,` prefix if present), collects `text` parts into a flat string, and emits `{"role":..., "content": "<joined text>", "images": ["<base64>", ...]}`.  Plain string-content messages pass through unchanged.  Translation runs on every `messages[]` element before the request body is built.
- **Prevention:**
  1. **Every backend that claims `Modality.VISION` must accept the canonical OpenAI multimodal content-array shape OR translate from it.**  The router emits one canonical format (#183); each backend either matches that format natively or owns the translation.
  2. **Vision capability declaration is a contract, not a hint.**  If `OllamaBackend.capabilities` returns `{VISION}` for a vision-trained model, the backend must successfully serve a vision turn — not 400.  The new `tests/test_router_routing.py` covers routing logic; vision-actually-works integration is exercised by `tests/e2e/runner.py story_full` (Tab5 photo → /api/media/upload → router → MiniCPM-V → reply).

### 91. Long-poll on ESP-IDF httpd is a footgun (PANIC under sustained load)
- **Date:** 2026-04-27
- **Symptom:** Tab5 e2e harness ran a `story_smoke` scenario with the experimental `?block_until=&timeout_ms=` long-poll variant of `/events`. Smoke completed but Tab5 became unreachable on the next run. `GET /info` after watchdog reset returned `"reset_reason":"PANIC"`.
- **Root Cause:** ESP-IDF httpd is single-task by design — one `httpd_task` services all connections via `select()`. When the long-poll handler `vTaskDelay`s waiting for a matching event, all other requests queue behind it. Worse, the experimental implementation re-allocated the cJSON events array on every 200 ms iteration of the wait loop. Across a multi-minute test run, the per-iteration alloc/free churn (or possibly the `cJSON` walks themselves) accumulated heap fragmentation that eventually pushed the device into PANIC.
- **Fix:** Reverted the long-poll feature entirely. `GET /events?since=N` stays instant-return only. Documented the caveat in `debug_server.c` so the next person who reaches for "but long-poll would be nice" sees the prior art. Harness in `tests/e2e/driver.py` falls back to 250 ms polling — burns more HTTP round-trips but doesn't risk the device.
- **Prevention:**
  1. **Single-thread http servers cannot host long-poll handlers.**  vTaskDelay inside a request handler is equivalent to taking the whole server offline for the duration.  If you need push-style notification, use a different transport (a dedicated WebSocket on a separate task, server-sent events with a per-request task spawned via `httpd_queue_work`, or just tell the client to poll faster).
  2. **Per-iteration heap allocation in tight loops on embedded devices is suspicious.**  Even if each iteration is balanced (alloc + free), the TLSF heap can fragment.  Allocate-once + reuse, or batch the work outside the loop.
  3. **"Reverted" is a valid commit message body.**  Documenting *why* a feature was removed (with the reset_reason and the timing) saves the next contributor from re-implementing it the same way.

### 92. Diagnostic side-snapshots steal events from the cursor that subsequent waiters need
- **Date:** 2026-04-27
- **Symptom:** Tab5 e2e harness's `await_event("camera.capture", timeout_s=10)` failed even though `/screenshot` clearly showed "Photo saved!" toast (event obviously fired).  Direct `/events?since=0` query confirmed `camera.capture` was in the ring.
- **Root Cause:** `Tab5Driver.events()` advances `_last_event_ms` to the latest event's timestamp on every call, so the next call only returns *newer* events.  The scenario runner's per-step diagnostic block called `events(since_ms=events_before)` to capture what fired during the step — and inadvertently advanced the cursor *past* the events it captured.  The next step's `await_event` started polling from a cursor that was already in the future relative to the events it was looking for.
- **Fix:** Added `peek=True` parameter to `events()`.  When `peek=True`, the cursor doesn't advance.  The scenario runner's diagnostic snapshot uses `peek=True`; explicit await calls still advance the cursor.
- **Prevention:**
  1. **Stateful side effects in "read" methods are easy to write and hard to debug.**  Whenever a read advances a cursor, give callers an opt-out for the side effect.
  2. **Diagnostic plumbing should never compete with primary control plumbing.**  Capturing "what happened" for a report is observation; consuming events for routing is action.  Separate the two.

### 93. claude-3.5-haiku via OpenRouter's Bedrock route rejects images at runtime
- **Date:** 2026-04-27
- **Symptom:** Router correctly picked `anthropic/claude-3.5-haiku` for a vision turn in cloud mode (the registry declared it vision-capable per Anthropic's published specs).  OpenRouter returned: `{"error":{"code":400,"metadata":{"raw":"{\"message\":\"'claude-3-5-haiku-20241022' does not support image input.\"}","provider_name":"Amazon Bedrock"}}}`.
- **Root Cause:** OpenRouter brokers the same model across multiple providers (Anthropic-direct, Amazon Bedrock, Google Vertex, etc.). Bedrock's claude-3.5-haiku endpoint is text-only even though Anthropic's native endpoint serves images. Our capability registry was based on the Anthropic-native truth — wrong for the Bedrock route OpenRouter happened to pick.
- **Fix:** Downgraded `anthropic/claude-3.5-haiku` to `{TEXT, TOOL_CALLING}` in `_OPENROUTER_CAPS` (PR #187). Vision-capable cheap-tier alternatives in the curated fleet: `qwen/qwen3.6-flash` ($0.25/$1.50, 1M ctx) and `google/gemini-3-flash-preview` ($0.50/$3, native video).
- **Prevention:**
  1. **OpenRouter capability declarations should be based on the route OR is most likely to pick, not the model's published native capabilities.**  When in doubt, conservatively declare text-only and let the user opt back in.
  2. **Add an integration test that hits each declared-vision-capable OR model with a tiny test image** — would have caught the Bedrock mismatch before the e2e harness did.  Parking this as a follow-up.

### 94. Ollama 0.21+ hides reasoning-model output behind `<think>` — visible content empty unless `think:false`
- **Date:** 2026-04-28 (closes #84)
- **Symptom:** `qwen3:1.7b` (and the rest of the qwen3 family + deepseek-r1, qwq) returned empty replies on Dragon — `'response': ''` from `/api/generate`, `'content': ''` from `/api/chat`.  CLAUDE.md's 2026-04-24 benchmark row credited qwen3:1.7b with 5/10 visible replies; current behaviour was 0/10.  No errors logged; just empty strings flowing into the wrap-empty-reply guard, which can't help here because there's no tool result to wrap either.
- **Root Cause:** Ollama 0.21 changed the response shape for reasoning models — it now strips the `<think>...</think>` block from output into a separate `thinking` field, leaving only the post-think answer in `response`/`content`.  qwen3 (and other reasoning-trained families) emit ALL their output as thinking until they reach a conclusion, then start the visible answer.  With our default `num_predict` budget of 128-256 tokens, the model runs out of budget mid-thought; visible content is empty.  Pre-0.21, the same model "worked" because `<think>` content streamed into `response` along with the answer — the user saw chain-of-thought + answer, ugly but non-empty.
- **Fix:** Added `_REASONING_MODEL_HINTS` substring registry (`qwen3`, `deepseek-r1`, `qwq`, `phi4-reasoning`) + `_is_reasoning_model()` helper in `dragon_voice/llm/ollama_llm.py`.  `OllamaBackend.__init__` flags itself with `_send_think_false=True` on reasoning models; both payload sites (`generate_stream` and `generate_stream_with_messages`) include `"think": False` when the flag is set.  Non-reasoning models never see the field (Ollama tolerates it as a no-op anyway, so the worst case is harmless).  Live verified on Dragon: `qwen3:1.7b` → `'Hello.'` in 0.23 s; `ministral-3:3b` unchanged.
- **Prevention:**
  1. **Backend behavior is coupled to its server version, not just its API surface.**  Ollama 0.21 broke a previously-working model class without any docs change on our side; we caught it because qwen3 was already in the bench table.  Keep the bench table dated and re-run after major Ollama upgrades — drift between "what the table says" and "what the server does" is real signal.
  2. **`think:false` is harmless on non-reasoning models** — Ollama silently ignores unknown top-level fields.  When in doubt about a per-model behavior knob, send it conditionally rather than per-config; the registry is the source of truth, not the user's settings file.
  3. **Heuristic-by-substring registries beat exhaustive-model-list registries for fast-moving model ecosystems.**  We can't list every reasoning model that will ever ship; we can list the *families* (qwen3, deepseek-r1, qwq) and accept the occasional false-positive (which costs nothing because Ollama ignores the field for non-reasoning models).

### 95. Tool-call observability: instrument at ToolRegistry.execute, not at WS hooks

- **Date:** 2026-05-01 (TT #328 Wave 12)
- **Symptom:** Wave 12 needed a cross-session "what tools has the agent run" feed for Tab5's Agents screen.  First-pass implementation hooked `record_call`/`record_result` into `_on_tool_call` / `_on_tool_result` in `server.py` (the existing WS handler hook points used to emit `tool_call`/`tool_result` events to the client).  Smoke test: trigger a tool via `POST /api/v1/tools/datetime/execute` → ring buffer empty.
- **Root Cause:** The WS handler hooks fire only for tools that flow through `ConversationEngine.process_text_stream()`.  Direct REST tool execution (`/api/v1/tools/{name}/execute`) bypasses the conversation engine and calls `ToolRegistry.execute(name, args)` directly.  Same for dashboard-triggered tools, MCP tools, and any future scheduler-triggered tools.  The WS hook was the wrong instrumentation surface — it only caught the WS conversation path.
- **Fix:** Moved instrumentation to `dragon_voice/tools/registry.py:execute()` — the canonical chokepoint EVERY tool invocation funnels through.  REST tool-execute, WS conversations, dashboard direct-execute, MCP bridge — all go through `ToolRegistry.execute`.  Single instrumentation site → uniform coverage with no per-caller plumbing.  Live verified by triggering tools via 3 different paths and observing all 3 in the ring.
- **Prevention:**
  1. **Before instrumenting a "things X happened" surface, find the chokepoint.**  Ask: "is there ONE function every X-event passes through?"  If yes, instrument there; if no, you'll be chasing every new caller forever.  In our codebase the rule of thumb is: instrument at the registry layer, not at the per-caller layer.
  2. **A WS handler hook for client-facing events is NOT the same as a global capture hook.**  WS hooks are about real-time client feedback (the user sees the tool fire); registry chokepoints are about historical observability (the system knows the tool fired, regardless of who asked).  Both are valid; pick based on the use case.
  3. **Wrap instrumentation calls in try/except.**  Tool execution must NEVER fail because of an instrumentation hiccup.  See `tools/registry.py:execute` — every `record_call` / `record_result` call is wrapped, and a failure logs at debug level + continues.  The tool result is the contract; the agent_log entry is best-effort.
