# Contributing to TinkerBox

> Thanks for showing up.  This doc covers the workflow, the rules,
> and the gotchas — most of which are also in [`CLAUDE.md`](CLAUDE.md)
> but consolidated here so a new contributor doesn't have to read
> 850 lines to figure out the branch-naming convention.

## TL;DR

- **One concern per PR.**  Refactor, fix, and feature changes don't share commits.
- **Issue first.**  Open a GitHub issue before opening a PR.  Title the issue clearly; the PR title can match.
- **Branch from `main`.**  Names: `feat/<slug>` / `fix/<slug>` / `chore/<slug>` / `docs/<slug>` / `investigate/<slug>`.
- **Conventional-commit prefix** on the subject (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- **Reference the issue:** `closes #N` to auto-close, `refs #N` to link without closing.
- **Squash-merge.**  No merge commits in `main`.  Delete the branch after merge.
- **Don't force-push shared branches.**  Force-push your own feature branch is fine.
- **Run the local lint + tests before pushing.**  See "Local pre-push" below.

## First time? Read these in order

1. [`WELCOME.md`](WELCOME.md) — multi-audience landing
2. [`docs/dev-setup.md`](docs/dev-setup.md) — get your local environment able to run + iterate
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system overview so you know where your change lives
4. [`LEARNINGS.md`](LEARNINGS.md) — search before you debug; we've probably already hit your bug

## Workflow

### 1. Open an issue first

```
gh issue create --title "feat: short description" --body "..."
```

Use the issue body to describe:
- **What:** the change you want to make
- **Why:** the problem or use case driving it
- **Approach:** how you plan to do it (so we can sanity-check before you write code)

For bug reports, use the format from CLAUDE.md:
```markdown
## Bug / Problem
What the user sees. Symptoms, frequency, impact.

## Root Cause
Technical explanation of WHY this happens.

## Culprit
Exact file(s) and line(s) responsible.

## Fix
What was changed and why this fixes it.

## Resolved
Commit hash + PR if applicable.
```

### 2. Branch from `main`

```bash
git checkout main && git pull --ff-only origin main
git checkout -b feat/my-thing
```

Branch-name convention:
- `feat/<slug>` — new feature
- `fix/<slug>` — bug fix
- `chore/<slug>` — repo maintenance, dep bumps
- `docs/<slug>` — docs only
- `investigate/<slug>` — exploration that may or may not ship
- `refactor/<slug>` — restructure without behavior change
- `test/<slug>` — add/fix tests

Cross-stack audit items already have Wave IDs (`W14-C01`, `W15-H09`, …) — reuse them in the branch and commit message instead of opening a duplicate issue.

### 3. Commit conventions

Conventional-commit prefix on the **subject line**, then a blank line, then the body explaining *why* (the *what* should already be in the diff).

```
feat(llm): refresh OpenRouter model registry to live 2026-04-27 catalog (refs #183)

Pulled the live OpenRouter /api/v1/models endpoint and updated both
the capability registry and the pricing table to current reality.

[Body explains the why, references prior PRs, etc.]

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Reference issues with `refs #N` to link, `closes #N` to auto-close on merge.

**Atomic commits.**  One logical change per commit.  The PR can have multiple commits but a reviewer should be able to skim each one independently.

### 4. PR scope discipline (the rule that saves review hours)

- **One concern per PR.**  Refactors don't contain bug fixes.  Bug fixes don't contain "while we're here" cleanups.  Doc updates don't contain behavior changes.  If you find something else that needs fixing mid-PR, open a new issue and move on.
- **Extract before decompose.**  When splitting a large file, the first PR *moves* code to its new home with identical behavior — no restructuring of internals.  Decomposing internals is follow-up PRs.
- **Tests move with code.**  If you're extracting a function that has tests, the tests move in the same commit.  If it has no tests, add at least one before extraction lands.
- **Small is kind.**  Prefer 5 small PRs over 1 big one.

### 5. Open the PR

```bash
gh pr create --title "feat(llm): one-line summary (refs #N)" --body "..."
```

PR description should include:
- **Summary:** 1-3 sentences of what changed
- **Why:** the problem the change solves
- **Test plan:** checkboxes for what you ran/tested
- **Screenshots** if UI work

Use `gh pr view` to monitor CI gates (see below).

### 6. Squash-merge + delete branch

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull --ff-only origin main
```

We **always squash-merge** on `main`.  No merge commits.  This keeps the history bisectable: every commit on `main` is a green PR.

## CI gates (enforced by `.github/workflows/ci.yml`)

- **Ruff** with a narrow code list:
  ```
  F821, F722, F811, F823, B006, B904, E722, B007, RUF006
  ```
  This is the *real-bug* gate — not a style gate.  Adding codes is a two-line change to `ci.yml`; prove a code catches a real bug before promoting it.
- **Named unit tests only.**  E2E tests (`test_api_e2e.py`, `test_e2e_dragon.py`) run locally, not in CI.  When you add a new test file that can run without a live server, add it to the CI test list in `ci.yml`.
- **CI uses `DRAGON_API_TOKEN=ci-bearer-token` + `TINKERCLAW_TOKEN=ci-tc-token`.**  Tests that need tokens must read them from env, not hardcoded.

## Local pre-push (takes ~10 seconds)

```bash
ruff check --select F821,F722,F811,F823,B006,B904,E722,B007,RUF006 dragon_voice/ dashboard.py tests/
pytest -q tests/test_auth_middleware.py tests/test_media_pipeline.py tests/test_session_cas.py
```

Pick the test files relevant to your change.  If you touched middleware: `test_auth_middleware` + `test_security_headers` + `test_rate_limit`.  Media: `test_media_*`.  LLM/router: `test_capability_declaration`, `test_router_routing`, `test_multimodal_persistence`.

The full local suite (excluding the audit/async one) is ~556 tests in 11 seconds:
```bash
python3 -m pytest tests/ --ignore=tests/audit -q
```

Don't run the e2e (`tests/test_api_e2e.py`) suite unless you've booted a local server.

## Anti-slop rules

These apply to humans and AI contributors equally:

- **No defensive code for impossible scenarios.**  Trust internal callers.  Validate at system boundaries (HTTP request, WS frame, NVS read) — not between two functions in the same module.
- **Delete, don't comment out.**  Git remembers.  `# TODO: remove this` is a lie; either fix it in this PR or open an issue.
- **No comments that restate well-named code.**  Comments exist to explain *why*, or to warn about non-obvious constraints.  `# increment counter` above `counter += 1` is noise.
- **No speculative abstractions.**  A factory class with one caller is a one-caller class pretending to be a factory.  Build it when the second caller arrives.
- **No "helpful" refactors next to the feature.**  If it's worth doing, it's worth a separate PR.  If it isn't, drop it.
- **Name things for the reader, not the writer.**  Method names describe what the caller gets; variable names describe what the thing *is*.  `_handle_ws_voice` beats `_process_incoming_voice_socket_request_with_fallback`.
- **`LEARNINGS.md` is not optional.**  Every bug fix with a non-obvious root cause adds an entry with Date / Symptom / Root Cause / Fix / Prevention.  Skip only if the fix is genuinely one-line and self-explaining.

## File-split smell test (for refactoring PRs)

A file is too big when it has more than one *reason to change*.  Before extracting, answer: "what stakeholder cares about the code I'm moving?"  If it's the same stakeholder as the rest of the file, don't extract yet.

Good candidates:
- middleware (ops/security)
- debug endpoints (dev/diagnostics)
- lifecycle (ops/reliability)
- business endpoints (product)

Reference: the [`server.py` decomposition (#65)](https://github.com/lorcan35/TinkerBox/pull/65) split the 2,747-LOC monolith into `middleware/`, `handlers/`, `lifecycle/`, and a slimmed `server.py`.

## Specific contribution recipes

| I want to… | Read this |
|------------|-----------|
| Add a new agentic tool | [`docs/adding-a-tool.md`](docs/adding-a-tool.md) |
| Add a new LLM model to the fleet | [`docs/router-cookbook.md`](docs/router-cookbook.md) "Adding a new OpenRouter model" |
| Author a skill that emits widgets | [`docs/SKILL_AUTHORING.md`](docs/SKILL_AUTHORING.md) + TinkerTab's [`docs/WIDGETS.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/WIDGETS.md) |
| Add a channel adapter (Telegram, Slack, etc.) | Existing example: [`docs/telegram-bot.md`](docs/telegram-bot.md) — minimal aiohttp polling bot |
| Add a STT or TTS backend | Subclass `dragon_voice/stt/base.py` or `dragon_voice/tts/base.py`; register in `__init__.py`; add tests |
| Find a bug in the WS dispatcher | Read [`docs/UX-GAPS.md`](docs/UX-GAPS.md) Phase 1 first — many WS dispatcher gotchas are catalogued |
| Run the e2e harness | [TinkerTab `tests/e2e/README.md`](https://github.com/lorcan35/TinkerTab/blob/main/tests/e2e/README.md) — Python harness drives Tab5 over the debug API |

## Cross-stack changes (touching both repos)

Many features touch both Dragon (this repo) and Tab5 firmware (TinkerTab).  Coordinate as follows:

1. Open an issue in **whichever repo's the harder side** of the change.  Cross-link the other repo's issue in the body.
2. PR in TinkerBox first if Dragon needs to support a new field.  Tab5 firmware can ignore unknown fields per the protocol's forward-compat design.
3. PR in TinkerTab next, depending on the merged TinkerBox change.
4. Update [`docs/protocol.md`](docs/protocol.md) **in the same PR as the protocol-changing side**, not as a follow-up.

## Where to ask questions

- **Architecture / design:** open a `[Q]`-prefixed issue.
- **"How do I do X?":** read [`WELCOME.md`](WELCOME.md) → relevant track first; if that doesn't answer, open a `[Q]` issue.
- **Stuck on a build error:** check [`LEARNINGS.md`](LEARNINGS.md) — there are 90+ entries of post-mortems.

## License

By contributing, you agree your contributions are licensed under the same license as the project (see `LICENSE`).
