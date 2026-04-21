# Wave 14 — Progress Tracker

Canonical status for every item in [docs/AUDIT.md](AUDIT.md). Update in the
same commit that closes an item. Format: one line per ID with status, branch
name, PR#, and one-sentence proof.

**Legend:** `[ ]` open · `[~]` in progress · `[x]` closed · `[-]` deferred

**Branches:** `feat/audit-wave-14` in both repos. Landing as multiple small
PRs rather than a single monolith so each phase can be reverted independently.

---

## Phase 1 — Unblock (required before anything else)

Each of these removes friction that slows everything after it.

- [x] **W14-H15** `[OPS]` gitignore + `git rm --cached` `dependencies.lock` → green TinkerTab CI · TinkerTab `81ce620` · verified: local rebuild clean, regenerated lock has only expected patch bumps
- [x] **W14-M16** `[OPS]` ruff gate expansion `B904,E722,B007,RUF006` + land the 17 prep fixes · TinkerBox · verified: `ruff check --select ...` all pass + 52/52 pytest green
- [x] **W14-H16** `[OPS]` `scripts/deploy.sh` with snapshot rollback + auth-probed smoke · TinkerBox · verified: ran end-to-end against 192.168.1.91, /health polled + auth probe 401/200, rollback snapshot `/home/radxa/.deploy_rollback/20260421-143142/` populated
- [x] **W14-H17** `[OPS]` `tinkerclaw-backup.service` + `.timer` (hourly, 14-copy retention) · TinkerBox · verified: triggered once, stamp=20260421-103548, `tinkerclaw-4.7MB notes-28KB cfg-1.4MB`, timer active, next fire 11:01 UTC. Backup .db contains all state tables (devices/events/memory_facts/memory_facts_fts/memory_facts_vec/memory_chunks/memory_documents). rsync + sqlite3 apt-installed on Dragon.

## Phase 2 — CRITICALs

- [x] **W14-C01** `[TT]` `touch_ws.c:294-297` — copy string literal to stack buffer before `esp_transport_ws_send_raw` · verified: flashed, 20-tap stress, heap_min 21724696 stable, home screenshot renders clean
- [x] **W14-C02** `[TT]` `ota.c:90-91` — NULL-guard after second `esp_http_client_init` · verified: `/ota/check` exercised, Tab5 alive post-call
- [x] **W14-C03** `[TT]` `ui_notes.c:1066-1067` — NULL-guard on transcription-queue HTTP init · verified: transcribe queue task alive (4 notes pending), selftest wifi/voice_ws/sd_card all pass
- [x] **W14-C04** `[TB+TT]` authenticate `/ws/voice` register frame (bearer OR signed HMAC) · TB server.py `_handle_ws_voice` + TT sdkconfig/Kconfig/voice.c · verified: Dragon rejects no-bearer with HTTP 401, Tab5 flashed with matching token in sdkconfig.local reconnects inside 8 s, `active_connections=1` on Dragon, session resumed cleanly
- [x] **W14-C05** `[TB]` port `NotesDB` to `aiosqlite` (or uniform `asyncio.to_thread`) · TinkerBox `65c182f` · verified: 8/8 pytest + 5×CRUD sequential live (2→7→2 row cycle), updates/deletes all 200
- [x] **W14-C06** `[TB]` store + cancel 3 fire-and-forget `asyncio.create_task` sites · same commit as M16 · verified: NotesService._spawn_bg + conn_state["bg_tasks"] both wired; cancelled in shutdown/_handle_disconnect

## Phase 3 — HIGH security (Dragon)

- [x] **W14-H01** `[TB]` widen `config_to_dict` redaction predicate to include `token|password|secret` · 5/5 pytest + live `/api/config` both tokens show `***redacted***`
- [x] **W14-H02** `[TB]` sweep dashboard innerHTML interpolations through `escHtml`; move `d.id` out of inline `onclick` · 13 render sites + new `escAttr` helper · verified: stored `<img src=x onerror=alert()>` in device name, dashboard renders it as literal text (23 `escAttr()` + 3 `escHtml(d.name)` gates shipped)
- [x] **W14-H03** `[TB]` SSRF protection on `MediaPipeline.proxy_image` (reject loopback/link-local/RFC1918, cap redirects, running-byte-counter) · 13/13 pytest + stream-read refactor
- [x] **W14-H04** `[TB]` bind `/api/media/{id}` to session OR issue HMAC-signed URLs; widen id to 128 bits · 11/11 pytest + live 403/200/403 for unsigned/signed/tampered
- [x] **W14-H05** `[TB]` drop hardcoded `Access-Control-Allow-Origin: *` from `messages.py`/`completions.py`/`dashboard.py` · 3 sites · middleware's origin allowlist now decides

## Phase 4 — HIGH stability (firmware + server)

- [x] **W14-H06** `[TT]` unified suspended-task worker pattern (mode_switch / wifi / media_fetch / drawer_fetch) · new `main/task_worker.{h,c}` single-queue single-worker; 4 task families converted from one-shot-spawn-then-suspend to plain job fns enqueued onto the worker. Live proof: 20 mic taps, tasks count stable at 26 (pre-H06 would have been 46), psram_free −424 bytes (vs expected 160 KB leak). Home UI re-renders clean post-overlay-dismiss.
- [x] **W14-H07** `[TT]` bump stacks: `sd_record_task` + `playback_task_fn` to 8 KB, `heap_watchdog_task` to 4 KB · TinkerTab · verified: flashed, boots clean, 25 tasks live, wifi/dragon/voice connected, heap_min=21724236 stable, home screenshot clean
- [x] **W14-H08** `[TB]` `asyncio.to_thread` / `web.FileResponse` for 6 sync file-read sites · api/synthesize.py (ota_check + ota_firmware), api/system.py (meminfo+loadavg), tools/system_tool.py (3 /proc reads), tts/piper_tts.py (voice config). Ruff ASYNC230 now 0. Live /api/v1/system + /api/ota/check + system_info tool all green.
- [ ] **W14-H09** `[TB]` MediaStore offload (`cleanup` + `store`) via `to_thread`; add default ClientTimeout
- [x] **W14-H10** `[TB]` `purge_old_messages` batching (`LIMIT N` + `asyncio.sleep(0)`); `executemany` for ingest · db.py batched at 500 rows/loop with yield; pytest green
- [x] **W14-H11** `[TB]` `MemoryService` shared ClientSession lifecycle · memory.py._http_session + shutdown · live store+search+delete cycle green
- [x] **W14-H12** `[TB]` `MediaPipeline.close()` on shutdown (folded into H09) · wired in server._on_shutdown
- [x] **W14-H18** `[OPS]` `MemoryMax=4G MemoryHigh=3G` on voice service (bandaid); file the leak as separate issue · drop-in installed live: MemoryHigh=3221225472 MemoryMax=4294967296 TasksMax=512
- [x] **W14-M09** `[TB]` `cancel() + await` `_periodic_purge` (wave 13 H3 pattern) · also folded memory_monitor cancel+await
- [x] **W14-H13** `[TB]` narrow ~17 residual `except Exception` in server/memory/media/tts · media/pipeline.py (4 sites narrowed to (OSError, ValueError, RuntimeError) + Pygments ClassNotFound), memory.py (6 sites narrowed to sqlite3.Error / aiohttp.ClientError / TimeoutError per intent), tts/edge_tts_backend.py (aiohttp+OS-only). server.py logger.exception sites left broad — they already surface bugs at exception log level. 90/90 pytest green.
- [x] **W14-H14** `[OPS]` systemd hardening drop-ins for 6 units · 5 drop-ins installed (voice/dashboard/gateway/ngrok/mdns); iteration-2 notes for known-breaks (SystemCallFilter dropped, AF_NETLINK kept, mdns User=nobody not DynamicUser). systemd-analyze score dropped ~9.6 → 6.5 per unit. Tab5 still WS-connected through the restart cycle.
- [ ] **W14-H18** `[OPS]` `MemoryMax=4G MemoryHigh=3G` on voice service (bandaid); file the leak as separate issue

## Phase 5 — HIGH docs + protocol

- [x] **W14-H19** `[DOC]` reconcile `capabilities.widgets` / `widget_capability` / protocol.md §2.1 / §17.12 · §2.1 register now documents `capabilities.widgets` nested form; §17.12 rewritten as "wave-14 reconciliation note" pointing back; reference table updated
- [ ] **W14-H20** `[DOC]` `clear_history` → `clear` in TinkerTab/CLAUDE.md
- [x] **W14-H21** `[DOC]` delete `conn_mode` paragraph from TinkerBox/CLAUDE.md · removed from header + table row; voice_mode now includes 0|1|2|3
- [ ] **W14-H22** `[DOC]` extend TinkerTab/CLAUDE.md NVS keys table with 11 missing entries
- [x] **W14-H23** `[DOC]` endpoint count: pick one number or generate from code · canonical count is 47 REST endpoints; 3 stale callsites all updated with grep-from-code formula

## Phase 6 — MEDIUM

- [x] **W14-M01** `[TT]` mutex coverage for `voice_get_*` readers (copy-under-lock) · new `voice_get_{last_transcript,stt_text,llm_text}_copy` helpers; debug_server migrated (httpd task no longer races WS RX)
- [x] **W14-M02** `[TT]` move overflow log outside `s_play_mutex` · already outside mutex at voice.c:394 — no code change needed; audit was against a stale snapshot
- [x] **W14-M03** `[TT]` `volatile` on `s_ws` + `dragon_link.s_state` · both decls now `volatile`; use-after-free race on disconnect closed
- [x] **W14-M04** `[TT]` check `fread`/`fwrite` returns in WAV-header repair · all 4 fseek/fread/fwrite returns checked; garbage-on-short-read corruption vector closed
- [ ] **W14-M05** `[TB]` switch `serve_media` to `web.FileResponse` (folded into H08)
- [x] **W14-M06** `[TB]` response-headers middleware (CSP, X-CTO, X-Frame-Options, Referrer-Policy) · outermost middleware stamps on every response incl. 401s; 3 pytest cases; live curl -sI confirms all 4 headers on /health and 401 path.
- [ ] **W14-M07** `[TB]` TinkerClaw gateway SSE chunk validation + length cap
- [x] **W14-M08** `[TT]` log `receipt_attach`/`voice_async_*` OOM drops · `ESP_LOGW` on all 3 drop paths
- [ ] **W14-M09** `[TB]` `cancel() + await` `_periodic_purge` (wave 13 H3 pattern)
- [ ] **W14-M10** `[TB]` ClientTimeout on MediaPipeline session (folded into H09)
- [x] **W14-M11** `[TB]` call `config.validate()` at end of `load_config`; raise on error · load_config now raises ValueError with actionable message on invalid backend string
- [x] **W14-M12** `[TB]` `NotesDB.update` single UPDATE with COALESCE; log unknown keys · new `_UPDATABLE` frozenset + warning log on dropped keys (atomic write under _write_lock preserved)
- [ ] **W14-M13** `[TB]` route `PiperBackend._ensure_model` consistently through executor
- [x] **W14-M14** `[TB]` `datetime.now(timezone.utc)` + `ClassVar[frozenset]` for CORS allowlist · datetime_tool.py + timer_tool.py tz-aware; `_CORS_ALLOWED_ORIGINS` now `ClassVar[frozenset]`
- [ ] **W14-M15** `[TB]` type annotations on DI entry points; `mypy --strict` on `api/`
- [ ] **W14-M17** `[OPS]` kill `tinkeraimcp` tunnel; basic_auth on dashboard+gateway
- [ ] **W14-M18** `[DOC]` update IDF pin references in TinkerTab/CLAUDE.md to 5.5.2
- [ ] **W14-M19** `[DOC]` update debug-server endpoint count 22 → 26 (or drop)
- [ ] **W14-M20** `[DOC]` update test counts: pipeline 28, aggregate 40
- [ ] **W14-M21** `[DOC]` canonical Dragon host: bump `test_api_e2e.py` default to `192.168.1.91`
- [ ] **W14-M22** `[DOC]` `settings.c:375` comment include mode 3

## Phase 7 — LOW

- [x] **W14-L01** `[TT]` delete dead `s_rec_paused` + TODO · variable + 3 callsites removed
- [x] **W14-L02** `[TT]` bounds-guard size_t→int cast in `voice_ws_send_text`/`_send_binary` · `len > INT_MAX` gate + NULL-arg reject
- [x] **W14-L03** `[TT]` observability log in `config_update` no-op branch · ESP_LOGD trail when neither voice_mode nor cloud_mode nor error is present
- [x] **W14-L04** `[TB]` clamp + try/except in `parse_pagination` · non-numeric limit no longer raises 500; negative offset clamped to 0. Live curl `?limit=abc` returns 200.
- [x] **W14-L05** `[TB]` `/api/ota/check` read canonical host from config · prefer version.json's `url` field; falls back to request.host for back-compat
- [x] **W14-L06** `[OPS]` logging.Filter redacting Bearer/sk- patterns · installed in `dragon_voice/__main__.py`; covers Bearer tokens, `sk-*` keys, and json `api_token`/`tinkerclaw_token` values
- [ ] **W14-L07** `[OPS]` `scripts/deploy-firmware.sh` atomic sha+json write
- [ ] **W14-L08** `[OPS]` `tinkerclaw-mdns` drop-in with `DynamicUser=true`
- [ ] **W14-L09** `[DOC]` Tab5 CLAUDE.md Key Files sweep against `ls main/`
- [ ] **W14-L10** `[DOC]` replace Recovery & Rollback section with tag-based rule
- [ ] **W14-L11** `[DOC]` protocol.md §2.1 add `capabilities.widgets` subsection (folded into H19)
- [x] **W14-L12** `[TB]` `conftest.py` for `test_foundation.py` scoped fixture · session-scoped `_session_db_root` autouse fixture in `tests/conftest.py`; CI's multi-file pytest invocation no longer accumulates /tmp noise

---

## Landed PRs

_(filled in as we close items — template: `PR#` · branch · items closed · evidence path)_

## Session log

_(append one line per closure with ID + evidence ref)_

**2026-04-21 14:10 — wave-14 branches created; tracker committed.**

**2026-04-21 15:14 — Phase 2b: W14-C05 NotesDB async port committed on feat/audit-wave-14-phase2b (65c182f), 8/8 pytest green, live CRUD green, all 6 CRITICALs now code-complete.**
