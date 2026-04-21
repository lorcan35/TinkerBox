# Wave 15 Cross-Stack Audit

**Kicked off:** 2026-04-21, immediately after Wave 14 closed at 63/64.
**Scope:** TinkerBox (Dragon voice server, Python/aiohttp) + TinkerTab (ESP32-P4 firmware, C/ESP-IDF 5.5.2).
**Driver:** Observed production regression — Dragon RSS climbs ~2 MB/min, trips the W14-H18 bandaid every ~25 min, Tab5 flashes "Dragon unreachable" during the cycle. One of the cycles left Tab5's debug httpd dead for the rest of the boot.

Numbering is continuous across repos per severity. `[TB]` = TinkerBox / Dragon server, `[TT]` = TinkerTab / Tab5 firmware, `[OPS]` = systemd / ngrok / deploy scripts, `[DOC]` = docs only.

## Pre-filed GitHub issues (also wave-15)
- `TinkerBox#50` — RSS leak → **W15-C01** (the real fix; W14-H18 was a bandaid)
- `TinkerBox#51` — mypy --strict on api/ → **W15-M14** (carryover from W14-M15)
- `TinkerTab#94` — Tab5 httpd dies after long Dragon outage → **W15-C02** (root cause found by audit)

---

## Phase 1 — Unblock (do before anything else)
These remove friction or fix the biggest observed user-visible failure first.

- **W15-C01** `[TB]` **Dragon RSS leak — ~2 MB/min drift, trips H18 bandaid every ~25 min** (issue #50). Root-cause candidates: aiohttp.ClientSession pools on backend swap, pygments tokenizer cache, event-bus subscribers after disconnect, moonshine/onnx retained state, SQLite WAL. Acceptance: 4 h soak, RSS stays within ±10% baseline, zero critical-threshold fires. Success criteria + instrumentation plan already in issue #50.

## Phase 2 — CRITICALs (user-visible breakage)

- **W15-C02** `[TT]` **Tab5 httpd handle never stored/stopped** — `main/debug_server.c:2009-2131` — server started via `httpd_start(&server, ...)` but `server` is local; if the Dragon-link restart path calls `httpd_stop` it has nothing to pass, so debug httpd becomes unreachable post-Dragon-outage (issue #94). Fix: store `static httpd_handle_t s_httpd` + wire `httpd_stop(s_httpd)` into shutdown.
- **W15-C03** `[TB]` **PIL Image.open leaks FDs on upload + render** — `api/media_routes.py:110`, `media/pipeline.py:483` — every upload and every table render leaks one FD. Fix: `with Image.open(...) as img:`.
- **W15-C04** `[TB]` **Backend-swap race on `ws.closed` check** — `server.py:1545` — `swap_backends()` then `if not ws.closed` then send; the close can happen between those lines. Hold `conn_lock` across the whole swap+response.

## Phase 3 — HIGH (silent corruption, crash, auth-adjacent)

### Server
- **W15-H01** `[TB]` **No rate limit on state-changing endpoints** — `api/sessions.py`, `api/devices.py` — DELETE device, POST session/end, POST session/pause. Add per-IP throttle.
- **W15-H02** `[TB]` **32 MB app limit vs 10 MB upload cap mismatch** — `server.py:98` + `api/media_routes.py:103` — 22 MB of junk is buffered before the in-code cap hits. Add `Content-Length` header check.
- **W15-H03** `[TB]` **`_handle_disconnect` bg-task cancel race** — `server.py:2374` — `speak_system` spawned by `cap_downgrade` can land after `gather(*bg_tasks)` and orphan. Hold `conn_lock` during gather.
- **W15-H04** `[TB]` **Image.open decode result not validated** — `api/media_routes.py:109` — `.size` access can raise on a partially-constructed Image. Extra try.
- **W15-H05** `[TB]` **Broad except in `pipeline.finish_dictation`** — `pipeline.py:348` — swallows dictation completion failures silently; user gets incomplete transcript with no warning. Narrow to `(ClientError, TimeoutError, ValueError)` + log session_id.
- **W15-H06** `[TB]` **SSE reconnect amplification on `/chat`** — tab-reopen can hit 30/s. Add `Retry-After` or dedup-token guard.
- **W15-H07** `[TB]` **WS upgrade 401 path lacks integration test** — `server.py:1110-1127` — no test exercises `_handle_ws_voice` without bearer, so a regression could ship silently. Add `test_ws_voice_rejects_unauthorized_upgrade`.

### Firmware
- **W15-H08** `[TT]` **Dictation buffer overflow** — `voice.c:661` — `cur_len` not bounded before append when transcript >64 KB. Buffer overrun corrupts WS state. Add `if (cur_len >= DICTATION_TEXT_SIZE-1) return;`.
- **W15-H09** `[TT]` **`widget_store` never freed on deinit** — `widget_store.c:24` — 32×widget_t + payloads retained across OTA reboot path. Add `widget_store_deinit()` + wire to shutdown.
- **W15-H10** `[TT]` **`strdup()` NULL unchecked in `ui_chat_push_message`** — `ui_chat.c:582` — OOM leaves dangling ptr in calloc'd struct; async callback derefs NULL. Guard + free-all on failure.

### Docs
- **W15-H11** `[DOC]` **LVGL expand pool docs drift** — `CLAUDE.md:325` says 1024 KB, `sdkconfig.defaults:117` is actually 4096 KB (total 4192 KB). Update.

---

## Phase 4 — MEDIUM (observability, correctness under edges)

### Server
- **W15-M01** `[TB]` **Memory monitor untested** — `server.py:577-645` — add `test_memory_monitor_triggers_gc` + `test_memory_monitor_restarts_pipelines`.
- **W15-M02** `[TB]` **`logger.exception` sites lack session_id/ws_id** — `server.py:1034/1544/1564/2282` — add `extra={...}` structured context.
- **W15-M03** `[TB]` **Pygments tokenizer cache never evicted** — `media/pipeline.py:321` — MB-scale leak over long runs (contributor to W15-C01). Evict after N renders.
- **W15-M04** `[TB]` **Pipeline holds `_on_event` closure after WS close** — `pipeline.py` — stale callback tries to call freed `conn_state`. Set `self._on_event = None` in `shutdown()`.
- **W15-M05** `[TB]` **Config validate misses empty backend strings** — `config.py:157-197` — `backend=""` bypasses the allowed-list check, crashes at first use. Add empty-string reject.
- **W15-M06** `[TB]` **Protocol drift on optional register fields** — `docs/protocol.md §2.1` — `battery_level`, `build_id` undocumented. Add "Optional fields" subsection.
- **W15-M07** `[TB]` **SQLite WAL autocheckpoint missing** — `db.py` — `PRAGMA wal_autocheckpoint=1000`.
- **W15-M08** `[TB]` **Text-path broad except loses exc type** — `server.py:2282` — narrow + log exc type.

### Firmware
- **W15-M09** `[TT]` **WS reconnect attempt counter unbounded** — `voice.c:183/1468/1541` — clamp `s_connect_attempt` at 10 to stop log noise at `attempt=999+`.
- **W15-M10** `[TT]` **Unchecked cJSON model field** — `voice.c:842` — fallback to `"local"` when Dragon omits model.
- **W15-M11** `[TT]` **`heap_watchdog_task` 4 KB stack insufficient for fragmentation-log path** — `heap_watchdog.c:282` — bump to 6 KB.
- **W15-M12** `[TT]` **`httpd_register_uri_handler` returns unchecked** — `debug_server.c:2098-2123` — 26 silent sites. Add `ESP_LOGW` on `!= ESP_OK` + surface in `/selftest`.
- **W15-M13** `[TT]` **NVS wear on config_update flood** — `settings.c:208` — debounce voice-mode writes at 1/s.

### Carryover
- **W15-M14** `[TB]` **mypy --strict on `api/`** (issue #51, carried from W14-M15). Full type annotations on DI entry points + CI gate.

### Firmware (more)
- **W15-M15** `[TT]` **`s_ws` access in `voice_ws_send_bin`/`_send_text` not mutex-guarded** — `voice.c:443/463/470` — wrap in `s_state_mutex`.
- **W15-M16** `[TT]` **`lv_async_call` receipt callback NULL-guard missing** — `voice.c:868/895` — gate behind feature flag or NULL-check the extern symbols.
- **W15-M17** `[TT]` **`touch_ws` retry without backoff** — `touch_ws.c:52-211` — exponential backoff matching `voice.c` (10s → 60s).
- **W15-M18** `[TT]` **`media_cache` URL bounds check missing** — `media_cache.c` — assert `strlen(url) < 256` + reject gracefully.
- **W15-M19** `[TT]` **heap_watchdog doesn't feed WDT during long ops** — `heap_watchdog.c` — `esp_task_wdt_reset()` inside the frag-scan loops.
- **W15-M20** `[TT]` **OTA `content_len` cap 1 KB too small for changelog** — `ota.c:70/108` — Dragon's version.json gained a 500 B changelog; bump cap to 4 KB.
- **W15-M21** `[TT]` **`widget_store` `lv_tick_get` called inside eviction loop** — `widget_store.c:58` — cache outside loop.

---

## Phase 5 — LOW (polish, docs, tests)

### Server
- **W15-L01** `[TB]` **`/sys/class/thermal` absent → misleading `temp=0`** — `server.py:593-608` — add debug log on missing thermal.
- **W15-L02** `[TB]` **No direct test for `config.validate` invalid backends** — add unit test.
- **W15-L03** `[TB]` **`_media_cleanup_loop` missing None-db guard** — `server.py:527` — `if self._db is None: break`.
- **W15-L04** `[TB]` **Empty CORS allowlist silently opens all origins** — `server.py:148` — warn loudly on startup if empty.
- **W15-L05** `[TB]` **No test for `_handle_disconnect` cleanup ordering** — mock each step + assert order.
- **W15-L06** `[TB]` **Ollama embeddings ClientTimeout missing** — `memory.py:59/170` — `ClientTimeout(total=10)`.
- **W15-L07** `[TB]` **`media/pipeline._get_http_session` silent ClientSession ctor exception** — log before re-raise.
- **W15-L08** `[TB]` **`/debug/*` widget endpoints security model undocumented** — CLAUDE.md note.

### Firmware
- **W15-L09** `[TT]` **CLAUDE.md "26 endpoints" drift** — replace with grep formula (same pattern as W14-M19).
- **W15-L10** `[TT]` **`s_play_running` missing `volatile`** — `voice.c:196` — add.
- **W15-L11** `[TT]` **`touch_ws` comment `#18` ambiguous** — clarify to "TinkerTab#18 TLSP cleanup crash".
- **W15-L12** `[TT]` **OTA NULL-body handling needs clarifying comment** — `ota.c:77`.
- **W15-L13** `[TT]` **Dictation buffer 65536 hardcoded** — `voice.c:147` — `TAB5_MAX_DICTATION_BYTES` config macro.

---

## Totals by phase and severity

| Phase | TB | TT | DOC | OPS | Total |
|-------|----|----|-----|-----|-------|
| 1 Unblock (C) | 1 | 0 | 0 | 0 | 1 |
| 2 CRITICALs | 2 | 1 | 0 | 0 | 3 |
| 3 HIGH | 7 | 3 | 1 | 0 | 11 |
| 4 MEDIUM | 8 (+1 carryover) | 13 | 0 | 0 | 22 |
| 5 LOW | 8 | 5 | 0 | 0 | 13 |
| **Total** | **27** | **22** | **1** | **0** | **50** |

---

## Branching + PR rhythm

Same as wave 14 — one feat branch per repo per phase, multi-item batches acceptable, each PR must include live-deploy proof. No monolith.

## Open questions for the operator
1. Should `W15-C01` (RSS leak) land first as the single Phase 1 item, or parallel with `C02` (Tab5 httpd orphan)? Both are user-visible. Recommendation: **C01 first**, because C02 is cosmetic-but-observable (debug server only, doesn't affect voice/chat), whereas C01 is the reason you saw "Dragon unreachable" earlier.
2. Any items from this list that look like red herrings or already-known-non-issues? A quick pass flag avoids us litigating them twice.
