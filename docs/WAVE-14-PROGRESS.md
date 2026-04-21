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

- [ ] **W14-H15** `[OPS]` gitignore + `git rm --cached` `dependencies.lock` → green TinkerTab CI
- [ ] **W14-M16** `[OPS]` ruff gate expansion `B904,E722,B007,RUF006` + land the 17 prep fixes
- [ ] **W14-H16** `[OPS]` `scripts/deploy.sh` with snapshot rollback + auth-probed smoke
- [ ] **W14-H17** `[OPS]` `tinkerclaw-backup.service` + `.timer` (hourly, 14-copy retention)

## Phase 2 — CRITICALs

- [ ] **W14-C01** `[TT]` `touch_ws.c:294-297` — copy string literal to stack buffer before `esp_transport_ws_send_raw`
- [ ] **W14-C02** `[TT]` `ota.c:90-91` — NULL-guard after second `esp_http_client_init`
- [ ] **W14-C03** `[TT]` `ui_notes.c:1066-1067` — NULL-guard on transcription-queue HTTP init
- [ ] **W14-C04** `[TB+TT]` authenticate `/ws/voice` register frame (bearer OR signed HMAC)
- [ ] **W14-C05** `[TB]` port `NotesDB` to `aiosqlite` (or uniform `asyncio.to_thread`)
- [ ] **W14-C06** `[TB]` store + cancel 3 fire-and-forget `asyncio.create_task` sites

## Phase 3 — HIGH security (Dragon)

- [ ] **W14-H01** `[TB]` widen `config_to_dict` redaction predicate to include `token|password|secret`
- [ ] **W14-H02** `[TB]` sweep dashboard innerHTML interpolations through `escHtml`; move `d.id` out of inline `onclick`
- [ ] **W14-H03** `[TB]` SSRF protection on `MediaPipeline.proxy_image` (reject loopback/link-local/RFC1918, cap redirects, running-byte-counter)
- [ ] **W14-H04** `[TB]` bind `/api/media/{id}` to session OR issue HMAC-signed URLs; widen id to 128 bits
- [ ] **W14-H05** `[TB]` drop hardcoded `Access-Control-Allow-Origin: *` from `messages.py`/`completions.py`/`dashboard.py`

## Phase 4 — HIGH stability (firmware + server)

- [ ] **W14-H06** `[TT]` unified suspended-task worker pattern (mode_switch / wifi / media_fetch / drawer_fetch)
- [ ] **W14-H07** `[TT]` bump stacks: `sd_record_task` + `playback_task_fn` to 8 KB, `heap_watchdog_task` to 4 KB
- [ ] **W14-H08** `[TB]` `asyncio.to_thread` / `web.FileResponse` for 6 sync file-read sites
- [ ] **W14-H09** `[TB]` MediaStore offload (`cleanup` + `store`) via `to_thread`; add default ClientTimeout
- [ ] **W14-H10** `[TB]` `purge_old_messages` batching (`LIMIT N` + `asyncio.sleep(0)`); `executemany` for ingest
- [ ] **W14-H11** `[TB]` `MemoryService` shared ClientSession lifecycle
- [ ] **W14-H12** `[TB]` `MediaPipeline.close()` on shutdown (folded into H09)
- [ ] **W14-H13** `[TB]` narrow ~17 residual `except Exception` in server/memory/media/tts
- [ ] **W14-H14** `[OPS]` systemd hardening drop-ins for 6 units
- [ ] **W14-H18** `[OPS]` `MemoryMax=4G MemoryHigh=3G` on voice service (bandaid); file the leak as separate issue

## Phase 5 — HIGH docs + protocol

- [ ] **W14-H19** `[DOC]` reconcile `capabilities.widgets` / `widget_capability` / protocol.md §2.1 / §17.12
- [ ] **W14-H20** `[DOC]` `clear_history` → `clear` in TinkerTab/CLAUDE.md
- [ ] **W14-H21** `[DOC]` delete `conn_mode` paragraph from TinkerBox/CLAUDE.md
- [ ] **W14-H22** `[DOC]` extend TinkerTab/CLAUDE.md NVS keys table with 11 missing entries
- [ ] **W14-H23** `[DOC]` endpoint count: pick one number or generate from code

## Phase 6 — MEDIUM

- [ ] **W14-M01** `[TT]` mutex coverage for `voice_get_*` readers (copy-under-lock)
- [ ] **W14-M02** `[TT]` move overflow log outside `s_play_mutex`
- [ ] **W14-M03** `[TT]` `volatile` on `s_ws` + `dragon_link.s_state`
- [ ] **W14-M04** `[TT]` check `fread`/`fwrite` returns in WAV-header repair
- [ ] **W14-M05** `[TB]` switch `serve_media` to `web.FileResponse` (folded into H08)
- [ ] **W14-M06** `[TB]` response-headers middleware (CSP, X-CTO, X-Frame-Options, Referrer-Policy)
- [ ] **W14-M07** `[TB]` TinkerClaw gateway SSE chunk validation + length cap
- [ ] **W14-M08** `[TT]` log `receipt_attach`/`voice_async_*` OOM drops
- [ ] **W14-M09** `[TB]` `cancel() + await` `_periodic_purge` (wave 13 H3 pattern)
- [ ] **W14-M10** `[TB]` ClientTimeout on MediaPipeline session (folded into H09)
- [ ] **W14-M11** `[TB]` call `config.validate()` at end of `load_config`; raise on error
- [ ] **W14-M12** `[TB]` `NotesDB.update` single UPDATE with COALESCE; log unknown keys
- [ ] **W14-M13** `[TB]` route `PiperBackend._ensure_model` consistently through executor
- [ ] **W14-M14** `[TB]` `datetime.now(timezone.utc)` + `ClassVar[frozenset]` for CORS allowlist
- [ ] **W14-M15** `[TB]` type annotations on DI entry points; `mypy --strict` on `api/`
- [ ] **W14-M17** `[OPS]` kill `tinkeraimcp` tunnel; basic_auth on dashboard+gateway
- [ ] **W14-M18** `[DOC]` update IDF pin references in TinkerTab/CLAUDE.md to 5.5.2
- [ ] **W14-M19** `[DOC]` update debug-server endpoint count 22 → 26 (or drop)
- [ ] **W14-M20** `[DOC]` update test counts: pipeline 28, aggregate 40
- [ ] **W14-M21** `[DOC]` canonical Dragon host: bump `test_api_e2e.py` default to `192.168.1.91`
- [ ] **W14-M22** `[DOC]` `settings.c:375` comment include mode 3

## Phase 7 — LOW

- [ ] **W14-L01** `[TT]` delete dead `s_rec_paused` + TODO
- [ ] **W14-L02** `[TT]` bounds-guard size_t→int cast in `voice_ws_send_text`/`_send_binary`
- [ ] **W14-L03** `[TT]` observability log in `config_update` no-op branch
- [ ] **W14-L04** `[TB]` clamp + try/except in `parse_pagination`
- [ ] **W14-L05** `[TB]` `/api/ota/check` read canonical host from config
- [ ] **W14-L06** `[OPS]` logging.Filter redacting Bearer/sk- patterns
- [ ] **W14-L07** `[OPS]` `scripts/deploy-firmware.sh` atomic sha+json write
- [ ] **W14-L08** `[OPS]` `tinkerclaw-mdns` drop-in with `DynamicUser=true`
- [ ] **W14-L09** `[DOC]` Tab5 CLAUDE.md Key Files sweep against `ls main/`
- [ ] **W14-L10** `[DOC]` replace Recovery & Rollback section with tag-based rule
- [ ] **W14-L11** `[DOC]` protocol.md §2.1 add `capabilities.widgets` subsection (folded into H19)
- [ ] **W14-L12** `[TB]` `conftest.py` for `test_foundation.py` scoped fixture

---

## Landed PRs

_(filled in as we close items — template: `PR#` · branch · items closed · evidence path)_

## Session log

_(append one line per closure with ID + evidence ref)_

**2026-04-21 14:10 — wave-14 branches created; tracker committed.**
