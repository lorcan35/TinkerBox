# Examples

Minimal templates for common contributor tasks. Each subfolder is a
self-contained example with a `README.md` explaining what it does
and how to integrate it.

| Folder | What it shows |
|--------|--------------|
| [`tool-hello-world/`](tool-hello-world/) | Smallest possible agentic tool — single file, fully tested, registers in 2 lines |

More examples land here as the skill platform matures. If you've
built something useful for a custom deployment, PR it as an example.

## Conventions

- Each example is dependency-free beyond what's in the main
  `requirements.txt`.
- Each example is opt-in — copying the file into `dragon_voice/` and
  registering it is the one-and-only step.
- Examples are not regression-tested in CI by default. If you want
  CI coverage, add the test path to `.github/workflows/ci.yml`.
- Examples are MIT-licensed via the project license.

## See also

- [`../docs/adding-a-tool.md`](../docs/adding-a-tool.md) — the full
  walkthrough using a `dice_roll` example
- [`../docs/SKILL_AUTHORING.md`](../docs/SKILL_AUTHORING.md) — for
  skills that emit widgets
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — workflow rules
