# TinkerBox docs

> **Part of the Tinker stack** — four repos, each documented on its own:
> [**TinkerTab**](https://github.com/lorcan35/TinkerTab) (Tab5 device firmware) ·
> [**TinkerBox**](https://github.com/lorcan35/TinkerBox) (Dragon inference server — "the brain") ·
> [**PingOS**](https://github.com/lorcan35/PingOS) (portable Tab5 OS) ·
> [**TinkerClaw**](https://github.com/lorcan35/TinkerClaw) (agent sidecar).
> New here? Each repo's README is its own front door.

This is the documentation map for **TinkerBox** — the Dragon-side server (the
brain). Docs follow [Diátaxis](https://diataxis.fr): four content types, each
with one job, never mixed. The [`tutorials/`](tutorials/), [`how-to/`](how-to/),
[`reference/`](reference/), and [`explanation/`](explanation/) directories are
the target structure; **most deep content lands in
[Wave 2](ROADMAP.md)** and the table below shows where each page lives today vs.
where it is headed.

## Standards

- [**`../STYLE.md`**](../STYLE.md) — how we write these docs (voice, Diátaxis rule, headers, naming).
- [**`../GLOSSARY.md`**](../GLOSSARY.md) — canonical cross-stack terms + TinkerBox-specific terms.
- [**`ROADMAP.md`**](ROADMAP.md) — the documentation program waves.
- [**`_templates/`**](_templates/) — the four Diátaxis page templates (start here when authoring).

## By Diátaxis type

### Tutorials (learning by doing — for tinkerers / end users)

| Page | Status |
|---|---|
| [`getting-started-user.md`](getting-started-user.md) | exists — to be reworked into `tutorials/getting-started.md` (Wave 2) |

### How-to guides (achieve a task — for developers / operators / integrators)

| Page | Status |
|---|---|
| [`dev-setup.md`](dev-setup.md) | exists — dev environment + deployment (Wave 2: split into `how-to/dev-setup.md` + `how-to/deploy.md`) |
| [`adding-a-tool.md`](adding-a-tool.md) | exists — author a new tool (Wave 2 → `how-to/`) |
| [`SKILL_AUTHORING.md`](SKILL_AUTHORING.md) | exists — author a skill/widget (Wave 2 → `how-to/`) |
| [`npu-setup.md`](npu-setup.md) | exists — Qualcomm NPU / QAIRT setup (Wave 2 → `how-to/`) |
| [`telegram-bot.md`](telegram-bot.md) | exists — Telegram channel setup (Wave 2 → `how-to/`) |

### Reference (look up facts — for integrators / developers)

| Page | Status |
|---|---|
| [`protocol.md`](protocol.md) | exists — the WebSocket protocol contract (Tab5 ↔ Dragon) |
| [`router-cookbook.md`](router-cookbook.md) | exists — multi-model router fleet recipes |
| REST API reference | planned (Wave 2 → `reference/`) — see CLAUDE.md "API-First Architecture" for the current surface |

### Explanation (understand why — for developers)

| Page | Status |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | exists — system overview, components, data flows (the map) |
| [`flows/voice-turn.md`](flows/voice-turn.md) · [`flows/vision-turn.md`](flows/vision-turn.md) · [`flows/video-call.md`](flows/video-call.md) | exist — per-flow traces |
| `explanation/how-the-stack-fits-together.md` | planned (Wave 2) — how the four repos relate, linked from the peer block |

## Not audience documentation

- [**`internal/`**](internal/) — plans, audits, RFCs, SOLID reviews. Moved out of `docs/` on 2026-05-29; see [`internal/README.md`](internal/README.md) for the old→new map.
- [**`historical/`**](historical/) — closed waves + superseded audits.
- [`CHANGELOG.md`](CHANGELOG.md) · [`release-notes.md`](release-notes.md) · [`MYPY-CLEAN-LIST.md`](MYPY-CLEAN-LIST.md) · [`UX-GAPS.md`](UX-GAPS.md) — project records and trackers.

## Operating the server

The deploy / debug / restart / monitor runbook lives in the repo root:
[`../CLAUDE.md`](../CLAUDE.md). For lessons + gotchas, [`../LEARNINGS.md`](../LEARNINGS.md).
