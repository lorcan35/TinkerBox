# Internal working docs

Plans, audits, RFCs, and SOLID/structural reviews — the working history of how
this server got built. **Moved here on 2026-05-29** from `docs/` so the
top-level `docs/` holds audience-facing documentation only.

These are **not** audience documentation. They are point-in-time engineering
artifacts (often superseded). For user-facing docs — tutorials, how-to guides,
reference, and explanation — see [`docs/README.md`](../README.md).

Git history is preserved across the move (`git mv`), so `git log --follow
docs/internal/<file>` still shows each doc's full history.

## What moved here (old path → new path)

| Old path | New path |
|---|---|
| `docs/PLAN-dual-model-pipeline.md` | `docs/internal/PLAN-dual-model-pipeline.md` |
| `docs/PLAN-tinkerbox-integrations.md` | `docs/internal/PLAN-tinkerbox-integrations.md` |
| `docs/AUDIT-solid-2026-05-03.md` | `docs/internal/AUDIT-solid-2026-05-03.md` |
| `docs/AUDIT-WAVE-15.md` | `docs/internal/AUDIT-WAVE-15.md` |
| `docs/SOLID-AUDIT.md` | `docs/internal/SOLID-AUDIT.md` |
| `docs/WAVE-15-PROGRESS.md` | `docs/internal/WAVE-15-PROGRESS.md` |
| `docs/RFC-scheduler.md` | `docs/internal/RFC-scheduler.md` |

## Not moved (still audience-facing or already archived)

- `docs/protocol.md`, `docs/ARCHITECTURE.md`, `docs/router-cookbook.md`,
  `docs/npu-setup.md` — Reference/Explanation candidates for Wave 2; they stay
  in `docs/`.
- `docs/historical/` — already-archived closed waves (Wave 14 audit + progress)
  keep their existing home; see [`docs/historical/README.md`](../historical/README.md).
