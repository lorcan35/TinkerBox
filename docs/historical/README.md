# TinkerBox — Historical Documents

Archive of audit + progress trackers for completed work waves. These
were load-bearing during their respective sprints but are now closed
and superseded; preserved here as institutional knowledge ("why did
we ship this fix the way we did?").

If something here contradicts CLAUDE.md or the live trackers in
`docs/`, **trust the live docs**.

## Index

| File | Era | Why archived |
|------|-----|--------------|
| `AUDIT-WAVE-14.md` | 2026-04-21 | Cross-stack findings audit (6 CRITICAL, 23 HIGH, 22 MEDIUM, 12 LOW = 63 items). All items have PR citations or "deferred to Wave 15+" markers. Wave 14 closed; current audit is `../AUDIT-WAVE-15.md`. |
| `WAVE-14-PROGRESS.md` | 2026-04-21 | Per-item checklist showing landing evidence for every Wave 14 audit item. Wave closed; current wave tracker is `../WAVE-15-PROGRESS.md`. |

## What's NOT in this folder (and why)

- **`AUDIT-WAVE-15.md` and `WAVE-15-PROGRESS.md`** — kept in `docs/`
  because Wave 15 is still active. Move them here once the wave
  closes.
- **`PLAN-dual-model-pipeline.md`** — the dual-model pipeline shipped
  but is opt-in only (Q6A RAM ceiling); kept in `docs/` as a
  reference for the validation results.
- **`router-cookbook.md`** — current canonical reference for fleet
  configs; living doc.
