# Contributing

Master Smith is one person's asset pipeline made public. Pull requests that make the models better are the ones
that matter most. Thank you for them.

## What helps most

- **Quality of the models.** Better part pictures, better registration, smarter assembly (fit, materials, edges,
  bore alignment, baking). Measure before and after on the same job - `out/<Name>/delivery/preview_views.png` from
  both runs - and put the renders in the PR. Passes that repair a finished mesh after the fact are not taken:
  build it right from the pictures instead.
- **New categories.** A skill is a Markdown file in `mastersmith/skills/` with front matter the tools read.
- **New meshers.** A vendor is one row in `mastersmith/pricing.py` and a payload in `ms.py`'s `_vendor_payload`;
  a local model is a runner in `mastersmith/local.py`. Unpriced endpoints are refused on purpose.
- **Engine importers.** `mastersmith/stages/package.py` writes the import notes; Unity and Godot get less love
  than Unreal today.

## How it is worked

There is no service: a coding agent (Claude Code, Codex, ...) runs in the repo root, reads `AGENTS.md` and drives
`python -m mastersmith.ms`. Read `AGENTS.md` and `.claude/skills/forge/SKILL.md` before changing the tools; the
rules there were learned on real builds and the dated notes in the code say why.

## Branches

`master` is what people run; `dev` is where work lands. Open pull requests against `dev`.

## Running the tests

```bash
python -m pytest -q tests                                      # pure tests, no keys, a few seconds
MASTERSMITH_BLENDER_TESTS=1 python -m pytest -q tests/test_assembly.py   # registration and assembly in Blender, minutes
```

Blender scripts cannot be unit-tested in full: run them on a real job (`ms assemble out/<Name>`) and look at the
six views before and after.
