# Wave 15 — Progress Tracker

Canonical status for every item in [docs/AUDIT-WAVE-15.md](AUDIT-WAVE-15.md). Update in the same commit that closes an item. Format: one line per ID with status, branch name, PR#, and one-sentence proof.

**Legend:** `[ ]` open · `[~]` in progress · `[x]` closed · `[-]` deferred

**Branches:** `feat/audit-wave-15` in both repos. Landing as multiple small PRs rather than a single monolith so each phase can be reverted independently.

---

## Phase 1 — Unblock

- [ ] **W15-C01** `[TB]` Dragon RSS leak (~2 MB/min) — the real fix for the W14-H18 bandaid. Instrument tracemalloc at 1 min / 10 min / 30 min / 60 min / 4 h, identify which long-lived object is accumulating, fix, 4 h soak verifies RSS stays within ±10% baseline. (GitHub `TinkerBox#50`.)

## Phase 2 — CRITICALs

- [ ] **W15-C02** `[TT]` Tab5 httpd handle never stored/stopped — `debug_server.c:2009-2131`. (GitHub `TinkerTab#94`.)
- [ ] **W15-C03** `[TB]` PIL `Image.open` leaks FDs on upload + render — `api/media_routes.py:110`, `media/pipeline.py:483`.
- [ ] **W15-C04** `[TB]` Backend-swap race on `ws.closed` check — `server.py:1545`.

## Phase 3 — HIGH

- [ ] **W15-H01** `[TB]` Rate limit state-changing endpoints
- [ ] **W15-H02** `[TB]` 32 MB app limit vs 10 MB upload cap mismatch
- [ ] **W15-H03** `[TB]` `_handle_disconnect` bg-task cancel race
- [ ] **W15-H04** `[TB]` `Image.open` decode result not validated
- [ ] **W15-H05** `[TB]` Broad except in `pipeline.finish_dictation`
- [ ] **W15-H06** `[TB]` SSE reconnect amplification on `/chat`
- [ ] **W15-H07** `[TB]` WS upgrade 401 path lacks integration test
- [ ] **W15-H08** `[TT]` Dictation buffer overflow in `handle_text_message`
- [ ] **W15-H09** `[TT]` `widget_store` never freed on deinit
- [ ] **W15-H10** `[TT]` `strdup` NULL unchecked in `ui_chat_push_message`
- [ ] **W15-H11** `[DOC]` LVGL expand pool docs drift (1024 → 4096 KB)

## Phase 4 — MEDIUM

- [ ] **W15-M01** `[TB]` Memory monitor untested
- [ ] **W15-M02** `[TB]` `logger.exception` sites lack session_id/ws_id context
- [ ] **W15-M03** `[TB]` Pygments tokenizer cache never evicted
- [ ] **W15-M04** `[TB]` Pipeline holds `_on_event` closure after WS close
- [ ] **W15-M05** `[TB]` Config validate misses empty backend strings
- [ ] **W15-M06** `[TB]` Protocol drift on optional register fields (`battery_level`, `build_id`)
- [ ] **W15-M07** `[TB]` SQLite WAL autocheckpoint missing
- [ ] **W15-M08** `[TB]` Text-path broad except loses exc type
- [ ] **W15-M09** `[TT]` WS reconnect attempt counter unbounded
- [ ] **W15-M10** `[TT]` Unchecked cJSON model field in receipt handler
- [ ] **W15-M11** `[TT]` `heap_watchdog_task` 4 KB → 6 KB
- [ ] **W15-M12** `[TT]` `httpd_register_uri_handler` returns unchecked (26 sites)
- [ ] **W15-M13** `[TT]` NVS wear on `config_update` flood
- [ ] **W15-M14** `[TB]` mypy --strict on `api/` (carryover W14-M15, issue `TinkerBox#51`)
- [ ] **W15-M15** `[TT]` `s_ws` access in `voice_ws_send_bin`/`_send_text` not mutex-guarded
- [ ] **W15-M16** `[TT]` `lv_async_call` receipt callback NULL-guard missing
- [ ] **W15-M17** `[TT]` `touch_ws` retry without backoff
- [ ] **W15-M18** `[TT]` `media_cache` URL bounds check missing
- [ ] **W15-M19** `[TT]` heap_watchdog doesn't feed WDT during long ops
- [ ] **W15-M20** `[TT]` OTA `content_len` cap 1 KB too small for changelog
- [ ] **W15-M21** `[TT]` `widget_store` `lv_tick_get` inside eviction loop

## Phase 5 — LOW

- [ ] **W15-L01** `[TB]` `/sys/class/thermal` absent → misleading `temp=0`
- [ ] **W15-L02** `[TB]` No direct test for `config.validate` invalid backends
- [ ] **W15-L03** `[TB]` `_media_cleanup_loop` missing None-db guard
- [ ] **W15-L04** `[TB]` Empty CORS allowlist silently opens all origins
- [ ] **W15-L05** `[TB]` No test for `_handle_disconnect` cleanup ordering
- [ ] **W15-L06** `[TB]` Ollama embeddings ClientTimeout missing
- [ ] **W15-L07** `[TB]` `media/pipeline._get_http_session` silent ClientSession ctor exception
- [ ] **W15-L08** `[TB]` `/debug/*` widget endpoints security model undocumented
- [ ] **W15-L09** `[TT]` CLAUDE.md "26 endpoints" drift (replace with grep formula)
- [ ] **W15-L10** `[TT]` `s_play_running` missing `volatile`
- [ ] **W15-L11** `[TT]` `touch_ws` comment `#18` ambiguous
- [ ] **W15-L12** `[TT]` OTA NULL-body handling needs clarifying comment
- [ ] **W15-L13** `[TT]` Dictation buffer 65536 hardcoded (make configurable)

---

## Landed PRs
_(filled in as we close items — template: `PR#` · branch · items closed · evidence path)_

## Session log
_(append date-prefixed notes as phases land)_
