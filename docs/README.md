# TinkerBox docs

> **Part of the Tinker stack** — four repos, each documented on its own:
> [**TinkerTab**](https://github.com/lorcan35/TinkerTab) (Tab5 device firmware) ·
> [**TinkerBox**](https://github.com/lorcan35/TinkerBox) (Dragon inference server — "the brain") ·
> [**PingOS**](https://github.com/lorcan35/PingOS) (website-to-API automation gateway) ·
> [**TinkerClaw**](https://github.com/lorcan35/TinkerClaw) (agent sidecar).
> New here? Each repo's README is its own front door.

This is the documentation map for **TinkerBox** — the Dragon-side server (the
brain). Docs follow [Diátaxis](https://diataxis.fr): four content types, each
with one job, never mixed. The [`tutorials/`](tutorials/), [`how-to/`](how-to/),
[`reference/`](reference/), and [`explanation/`](explanation/) directories hold
the [Wave 2](ROADMAP.md) content, indexed below by type and audience. A handful
of older top-level pages (`getting-started-user.md`, `dev-setup.md`, …) remain
in place and are linked from their type's section until they are folded in.

## Standards

- [**`../STYLE.md`**](../STYLE.md) — how we write these docs (voice, Diátaxis rule, headers, naming).
- [**`../GLOSSARY.md`**](../GLOSSARY.md) — canonical cross-stack terms + TinkerBox-specific terms.
- [**`ROADMAP.md`**](ROADMAP.md) — the documentation program waves.
- [**`_templates/`**](_templates/) — the four Diátaxis page templates (start here when authoring).

## By Diátaxis type

### Tutorials (learning by doing — for tinkerers / operators)

| Page | Audience | What it gets you |
|---|---|---|
| [`tutorials/get-dragon-running.md`](tutorials/get-dragon-running.md) | operator | A working Dragon server you can talk to — install, configure, first voice turn. |
| [`getting-started-user.md`](getting-started-user.md) | tinkerer | Older end-user walkthrough; superseded by the tutorial above, kept until folded in. |

### How-to guides (achieve a task — for operators / developers)

| Page | Audience | Task |
|---|---|---|
| [`how-to/deploy-on-a-dragon.md`](how-to/deploy-on-a-dragon.md) | operator | Push code to a Dragon and bring the services up under systemd. |
| [`how-to/swap-the-llm-backend.md`](how-to/swap-the-llm-backend.md) | operator | Change the active LLM backend (Ollama / llama-server / OpenRouter / TinkerClaw). |
| [`how-to/configure-the-multi-model-router.md`](how-to/configure-the-multi-model-router.md) | operator | Declare a backend fleet and route per-turn by modality + tier. |
| [`how-to/connect-an-integration.md`](how-to/connect-an-integration.md) | operator | Connect a Google integration (Calendar / Gmail / Tasks) via the REST connect flow. |
| [`how-to/run-the-tinkerclaw-sidecar.md`](how-to/run-the-tinkerclaw-sidecar.md) | operator | Run the TinkerClaw gateway and route voice mode 3 to it. |
| [`how-to/add-a-tool.md`](how-to/add-a-tool.md) | developer | Author and register a new agentic tool. |
| [`dev-setup.md`](dev-setup.md) | developer | Older dev-environment + deploy page; kept until split into `how-to/`. |
| [`adding-a-tool.md`](adding-a-tool.md) | developer | Original worked tool example; companion to `how-to/add-a-tool.md`. |
| [`SKILL_AUTHORING.md`](SKILL_AUTHORING.md) | developer | Author a skill/widget surface. |
| [`npu-setup.md`](npu-setup.md) | operator | Qualcomm NPU / QAIRT SDK setup. |
| [`telegram-bot.md`](telegram-bot.md) | operator | Telegram channel setup. |

### Reference (look up facts — for integrators / developers)

| Page | Audience | What it documents |
|---|---|---|
| [`reference/rest-api.md`](reference/rest-api.md) | integrator | The REST API surface (`/api/v1/*`) — endpoints, auth, payloads. |
| [`reference/websocket-protocol.md`](reference/websocket-protocol.md) | integrator | The Dragon-side WebSocket message catalog (Tab5 ↔ Dragon). |
| [`reference/config-reference.md`](reference/config-reference.md) | integrator | Every `config.yaml` field, scope, and default. |
| [`reference/voice-modes.md`](reference/voice-modes.md) | integrator | The six voice modes and their STT/LLM/TTS backend mapping. |
| [`reference/tools-catalog.md`](reference/tools-catalog.md) | integrator | The built-in tools, their args, and tool-call dialects. |
| [`reference/package-file-map.md`](reference/package-file-map.md) | developer | The `dragon_voice` package layout — module → responsibility. |
| [`protocol.md`](protocol.md) | integrator | The full canonical WebSocket protocol contract (source of truth). |
| [`router-cookbook.md`](router-cookbook.md) | integrator | Multi-model router fleet recipes. |

### Explanation (understand why — for developers)

| Page | Audience | The mental model |
|---|---|---|
| [`explanation/how-the-stack-fits-together.md`](explanation/how-the-stack-fits-together.md) | developer | How the four repos relate — Tab5 (face) ↔ Dragon (brain) ↔ gateway ↔ cloud. Linked from the peer block. |
| [`explanation/architecture.md`](explanation/architecture.md) | developer | Inside the brain — sessions, the append-only ledger, the `#65` decomposition. |
| [`explanation/the-voice-pipeline.md`](explanation/the-voice-pipeline.md) | developer | How one voice turn flows STT → ConvEngine → LLM → tools → TTS. |
| [`explanation/the-multi-model-router.md`](explanation/the-multi-model-router.md) | developer | Why routing is capability- and tier-aware, and how `choose()` works. |
| [`explanation/local-first-inference.md`](explanation/local-first-inference.md) | developer | Why Local mode is Dragon-only and how llama-server replaced Ollama. |
| [`explanation/memory-and-rag.md`](explanation/memory-and-rag.md) | developer | How facts + documents are embedded and recalled into context. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | developer | The system map — components, network topology, data flows. |
| [`flows/voice-turn.md`](flows/voice-turn.md) · [`flows/vision-turn.md`](flows/vision-turn.md) · [`flows/video-call.md`](flows/video-call.md) | developer | Per-flow code-level traces. |

## Not audience documentation

- [**`internal/`**](internal/) — plans, audits, RFCs, SOLID reviews. Moved out of `docs/` on 2026-05-29; see [`internal/README.md`](internal/README.md) for the old→new map.
- [**`historical/`**](historical/) — closed waves + superseded audits.
- [`CHANGELOG.md`](CHANGELOG.md) · [`release-notes.md`](release-notes.md) · [`MYPY-CLEAN-LIST.md`](MYPY-CLEAN-LIST.md) · [`UX-GAPS.md`](UX-GAPS.md) — project records and trackers.

## Operating the server

The deploy / debug / restart / monitor runbook lives in the repo root:
[`../CLAUDE.md`](../CLAUDE.md). For lessons + gotchas, [`../LEARNINGS.md`](../LEARNINGS.md).
