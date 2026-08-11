# Scout

Scout is a local-first discovery engine that turns bounded, attributable observations into fact-locked decisions and at most three concise cards per manual run.

## Current vertical slice

```text
Official NousResearch/hermes-agent releases
→ Observation
→ Event
→ authoritative Factual Gate
→ gpt-5.6-sol attention triage without tools
→ Decision
→ deterministic ranking
→ 0..3 local Cards
```

Zero Cards is a valid result. The model cannot rewrite locked facts, downgrade `MUST_SHOW`, add sources, or browse. Scout does not deliver to Discord in the current public slice.

## Repository boundary

This repository contains only code, tests, JSON Schemas, sanitized documentation, CI, and neutral configuration examples.

Personal runtime data belongs outside Git under:

```text
~/.local/state/scout/
```

This includes the real Profile, observations, Events, Cards, feedback, weekly reviews, checkpoints, source state, run history, staging, caches, and any private source data.

Raw Gate 1 artifacts are intentionally excluded. Only the sanitized aggregate summary is retained in [`docs/GATE1_SUMMARY.md`](docs/GATE1_SUMMARY.md).

## Requirements

- Python 3.11 or 3.12
- `jsonschema==4.10.3`
- a canonical local Hermes Agent installation for real Sol triage

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

## Configuration

Copy [`config/scout.example.json`](config/scout.example.json) outside the checkout and replace only the neutral example values:

```bash
install -d -m 700 "$HOME/.local/state/scout/runtime"
install -m 600 config/scout.example.json "$HOME/.local/state/scout/config.json"
```

The runtime config contains:

- an absolute `state_root` outside Git;
- a configurable `profile_id`;
- the private `profile_context` supplied to the attention-only model prompt.

## Manual run

A real run performs one bounded GET against the official Hermes releases endpoint. Sol is called only when the Factual Gate produces candidates requiring triage.

```bash
python3 scripts/run_step2.py --config "$HOME/.local/state/scout/config.json"
```

No scheduler, daemon, database, Web App, automatic delivery, feedback mutation, model cascade, or additional source is included.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q scout_mvp scripts tests
```

## Git workflow

After the one-time empty-repository bootstrap:

```text
branch → tests → independent review → push branch → PR → CI → validation → merge
```

No force-push or publication of the private R&D archive is required.
