# Contributing

Master Smith is one person's asset pipeline made public. Pull requests that make the models better are the ones
that matter most. Thank you for them.

## What helps most

- **Quality of the models.** Better reference pictures, better seed vendor settings, better assembly parts, smarter
  Blender finishing (glass, wheels, decimation, baking). Measure before and after on the same brief and put the
  renders in the PR. Passes that repair a finished mesh after the fact (recolouring under masks, deleting or fitting
  parts, repainting) are not taken: build it right from the pictures instead.
- **New categories.** A skill is a Markdown file in `mastersmith/skills/` with front matter the pipeline reads.
  See the README section "Adding a skill or a vendor".
- **New vendors.** One row in `mastersmith/pricing.py` and a call site in a stage. Unpriced endpoints are refused
  on purpose, so the row is not optional.
- **Engine importers.** `mastersmith/stages/package.py` writes the import notes; Unity and Godot get less love
  than Unreal today.
- **The chat.** `web/` is a Next.js app on the Vercel AI SDK; the director itself is Python (`mastersmith/agent.py`).

## Branches

`master` is what people run; `dev` is where work lands. Open pull requests against `dev`. When `dev` is verified
end to end (a real build through the containers), it is merged into `master`.

## Running the tests

```bash
python -m pytest -q tests/test_core.py tests/test_syntax.py       # pure tests, no keys, a second
MASTERSMITH_BLENDER_TESTS=1 python -m pytest -q tests/test_blender_synthetic.py   # needs Blender, about a minute
cd web && npm run build && npm run lint
```

A build that reaches fal or OpenRouter spends real money on your keys. Keep live runs out of tests.

## Style

Python: four-space indentation, one job per module, comments that say *why* (many carry the date and the asset that
taught the lesson; keep doing that). No formatter is enforced; `ruff`-clean is welcome. TypeScript: the scaffold's
ESLint config.

## Secrets

`.env` is ignored and must stay that way. Never paste a key, a signed URL or a customer picture into an issue.

## Names inside .blend files

Delivered `.blend` files carry the brief and the reference picture as datablocks named `ms_spec.json` and
`ms_reference`; the finish's scratch attributes and images are `ms_*` too. `blend_to_seed.py` also reads the names
early builds used, so keep both when you touch that script.
