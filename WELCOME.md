# Welcome to TinkerClaw

> Pick your track. The docs branch by *what you want to do*, not by
> which repo a file lives in.  All three tracks are first-class —
> the project is built for tinkerers and hackers as much as for
> people who just want a voice assistant that doesn't snitch on them.

```
┌─────────────────────────────────────────────────────────┐
│  I just want to USE it                                  │
│  → "Getting Started for Users" (5 minutes)              │
│    docs/getting-started-user.md                         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  I want to BUILD or extend it                           │
│  → "Dev setup" (30 min) → CONTRIBUTING → "Add a tool"   │
│    docs/dev-setup.md, CONTRIBUTING.md, docs/adding-a-tool.md
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  I want to LOOK UNDER THE HOOD                          │
│  → ARCHITECTURE → PROTOCOL → flows/ → SECURITY          │
│    docs/ARCHITECTURE.md, docs/protocol.md,              │
│    docs/flows/, SECURITY.md                             │
└─────────────────────────────────────────────────────────┘
```

---

## What is TinkerClaw?

A privacy-conscious voice assistant that runs on hardware you own.

You talk to a **5-inch portrait touchscreen** ("Tab5") sitting on your
desk or shelf.  All the intelligence — speech-to-text, the AI model
that thinks about your question, text-to-speech for the reply —
runs on **a small Linux box** ("Dragon Q6A") somewhere on your home
network.  Cloud AI providers (Claude, GPT, Gemini, etc.) are
*optional*; the default mode runs everything locally and never
touches the internet.

The product is voice-first.  It has a chat screen, a camera screen,
a notes app, and a settings screen — but the main thing is: tap the
glowing orb, ask a question, get a spoken answer.

Want video?  Yes — Tab5 has a camera, can send photos for analysis,
and can do bidirectional video calls with another Tab5 or with a
browser at `/call` on your Dragon.

Want skills/automations?  Yes — the system is designed around a
"skills" platform where authors emit typed widget state that Tab5
renders opinionatedly.  See [`docs/SKILL_AUTHORING.md`](docs/SKILL_AUTHORING.md).

Want to swap out which AI runs?  Yes — the multi-model router
([`docs/router-cookbook.md`](docs/router-cookbook.md)) picks per-turn
based on what the message needs.  Text → cheap fast model; vision →
vision-capable model; video → native-video model.  Mix local +
cloud + LAN tier (e.g., a workstation running LM Studio).

---

## Track 1 — "I just want to use it"

You've been handed (or bought) a Tab5 + Dragon.  Now what?

→ **Read [`docs/getting-started-user.md`](docs/getting-started-user.md)** — power-on to first conversation in plain English, plus what you can ask, what each voice mode does, what it costs, what privacy you get.

If you're stuck:
- Hardware questions about the Tab5 itself: [TinkerTab `docs/HARDWARE.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/HARDWARE.md)
- Voice not working: troubleshooting in [`docs/getting-started-user.md`](docs/getting-started-user.md) bottom section
- Worried about privacy: [`SECURITY.md`](SECURITY.md) "Data inventory" section explains what gets sent where

---

## Track 2 — "I want to build / extend / contribute"

You want to add a tool, a skill, a new LLM backend, a channel
adapter, or fix a bug.

→ **Start with [`docs/dev-setup.md`](docs/dev-setup.md)** — get your
local env able to run + flash + iterate.

Then in order:
1. [`CONTRIBUTING.md`](CONTRIBUTING.md) — workflow, branch naming, commit conventions, CI gates.
2. [`docs/adding-a-tool.md`](docs/adding-a-tool.md) — worked example: build a new agentic tool from scratch.
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system overview so you know where your change fits.
4. [`docs/SKILL_AUTHORING.md`](docs/SKILL_AUTHORING.md) — if you're emitting widgets to Tab5.
5. [`docs/router-cookbook.md`](docs/router-cookbook.md) — if you're plugging in a new LLM model.
6. [`tests/e2e/README.md`](https://github.com/lorcan35/TinkerTab/blob/main/tests/e2e/README.md) (in TinkerTab) — Python harness for end-to-end testing.

When you're stuck on a weird error: [`LEARNINGS.md`](LEARNINGS.md) is a long, brutal log of post-mortems. Search before you debug.

---

## Track 3 — "I want to look under the hood"

You're a security researcher, a protocol nerd, a "how does this thing
*actually* work" person.  Or you're auditing for a deployment.

→ **Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — the canonical map.

Then in order:
1. [`docs/protocol.md`](docs/protocol.md) — full WebSocket wire format.  Implement-from-this quality.
2. [`docs/flows/`](docs/flows/) — three end-to-end traces (voice, vision, video call) with file:line refs.
3. [`SECURITY.md`](SECURITY.md) — threat model, trust boundaries, auth-token lifecycle, data inventory, known limitations honestly stated.
4. [`GLOSSARY.md`](GLOSSARY.md) — when an unfamiliar term shows up.
5. [`LEARNINGS.md`](LEARNINGS.md) — gotchas, root-cause analyses, post-mortems.  Often more useful than reading the code.

For Tab5-side firmware:
- [TinkerTab `docs/HARDWARE.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/HARDWARE.md) — pinout + IC list
- [TinkerTab `SECURITY.md`](https://github.com/lorcan35/TinkerTab/blob/main/SECURITY.md) — firmware-specific surface

---

## How the docs are organised

Both repos follow the same convention:

```
README.md           — public-facing landing
WELCOME.md          — this file (multi-audience track index)
SECURITY.md         — threat model + disclosure
CONTRIBUTING.md     — how to contribute
GLOSSARY.md         — terminology
CLAUDE.md           — operational runbook (deploy, debug, restart, monitor)
LEARNINGS.md        — post-mortems and institutional knowledge

docs/
  ARCHITECTURE.md   — system overview (TinkerBox only — covers both halves)
  protocol.md       — WS wire format (TinkerBox only — single source of truth)
  HARDWARE.md       — Tab5 pinout (TinkerTab only)
  VOICE_PIPELINE.md — Tab5 voice chain (TinkerTab only)
  flows/            — end-to-end request traces (TinkerBox only)
  router-cookbook.md — fleet recipes (TinkerBox only)
  SKILL_AUTHORING.md — skill SDK reference (TinkerBox)
  WIDGETS.md        — widget design lock (TinkerTab)
  npu-setup.md      — Qualcomm NPU bring-up (TinkerBox)
  dev-setup.md      — developer environment setup (both)
  adding-a-tool.md  — tinkerer walkthrough (TinkerBox)
  getting-started-user.md — normie quickstart (TinkerBox)
  release-notes.md  — what's new (TinkerBox)
  historical/       — archived / superseded docs
  UI-COMPLETENESS.md — UI debt tracker (TinkerTab)
```

---

## What this project is NOT

Honest list, so you know what you're getting into:

- **Not a hardened appliance.**  Default deployment trusts the LAN, runs Tab5 firmware without secure boot, persists plaintext conversation history.  Fine for "home AI in my house"; not fine for "deploy this for users I don't know."  See [`SECURITY.md`](SECURITY.md).
- **Not a polished consumer product.**  The Tab5 hardware and the Dragon SBC are enthusiast hardware.  You'll need to be comfortable with USB flashing, SSH, systemd, and at least one programming language to operate this stack.
- **Not a managed service.**  No cloud control plane, no automatic updates, no support hotline.  When something breaks, you'll be reading logs.
- **Not a voice-only system.**  Despite the "voice-first" framing, there's a chat screen, a camera screen, a notes app, a video-call client.  Voice is the most important loop, not the only one.
- **Not committed to one LLM provider.**  The router design (#183-#188) deliberately makes it trivial to swap or mix models.  We don't lock in to Anthropic, OpenAI, Google, DeepSeek, or any other vendor.

---

## Companion projects

- [**TinkerTab**](https://github.com/lorcan35/TinkerTab) — the firmware running on the Tab5 hardware.  Pair with this repo.
- [**OpenClaw / TinkerClaw Gateway**](https://github.com/lorcan35/openclaw) — optional agentic sidecar for `voice_mode=3` (long-running autonomous tasks).

---

## Help

- Issues: file in the relevant repo (TinkerBox for Dragon, TinkerTab for Tab5).
- Security: see [`SECURITY.md`](SECURITY.md).
- Questions about the architecture: read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first; if it doesn't answer, file an issue with `[Q]` prefix.

Welcome aboard. 🐉
