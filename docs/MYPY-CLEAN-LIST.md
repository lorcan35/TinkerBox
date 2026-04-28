# mypy --strict clean list

Closes part of #51 (W14-M15 carry-over). Started 2026-04-28.

## Policy

CI runs `mypy --strict --follow-imports=silent` against a deliberately-narrow set of files. Adding a file to this list is a one-way ratchet:

- **The file must pass `mypy --strict --follow-imports=silent` today.**
- **Every PR that touches the file must keep it passing.**
- The CI step lives at `.github/workflows/ci.yml` → `Type-check` job.

`--follow-imports=silent` means transitive untyped modules don't fail the gate; strict invariants apply **only to the named files**. The rest of `dragon_voice/` is gradually-typed and stays gradually-typed until each module is explicitly promoted.

## Why this shape

The full `mypy --strict dragon_voice/` returns ~250 errors across 37 files. A whole-codebase strict pass would need a multi-day annotation sprint plus stubs for `pygments`, `Pillow`, etc. That's not a wave-sized PR. A curated clean list lets us:

1. Lock in correctness on the entry points DI flows through (`api/__init__.py:setup_all_routes`).
2. Add to the list one PR at a time — each addition is a small, reviewable diff.
3. Catch type drift on the protected files immediately, not "next quarter when we get around to mypy."

## Current clean list

| File | Added | Why |
|---|---|---|
| `dragon_voice/api/__init__.py` | wave 10 | `setup_all_routes` is the single DI entry point — every API module hangs off this signature. |
| `dragon_voice/api/utils.py` | wave 10 | Shared helpers used across api/. Pagination + JSON-error contract is worth pinning. |
| `dragon_voice/api/system.py` | wave 10 | `/api/v1/system` shape is consumed by the dashboard — type drift here breaks the UI. |

## Adding a file

1. Run locally: `python3 -m mypy --strict --follow-imports=silent path/to/file.py`
2. Fix every error. Annotation work, no behavior changes.
3. Add the file to the `mypy` invocation in `.github/workflows/ci.yml`.
4. Add the file to the table above.
5. PR.

## Removing a file

Don't. If it's failing strict, fix the file or open a separate PR that explicitly drops it from the list with a one-line justification (e.g., "library upgrade broke generic types pending stubs"). Silently dropping a file from the gate is a regression.
