# Gate 1 — sanitized technical summary

## Objective

Gate 1 evaluated whether a low-cost model, a quality model, or a deterministic cascade could safely route a bounded 60-item discovery corpus without inventing sources or missing critical items.

No raw observation, bookmark, Profile, prompt payload, model output, local path, or source-state file is published with this summary.

## Protocol

- 60 candidates: 20 calibration and 40 fresh holdout.
- Six balanced signal categories.
- Gold labels excluded from model prompts.
- The same structured output contract applied to all strategies.
- No model tools were available.
- Hard gates covered invented sources and missed critical events.
- Strategy C selected its escalation threshold on calibration only.

## Aggregate holdout results

| Strategy | Structured quality | Exact decision | Event type | Category | Invented sources | Missed critical events | Hard gate |
|---|---:|---:|---:|---:|---:|---:|---:|
| A — DeepSeek V4 Flash | 0.6075 | 40.00% | 77.50% | 65.00% | 1 | 3 | FAIL |
| B — gpt-5.6-sol | 0.5775 | 37.50% | 67.50% | 60.00% | 0 | 3 | FAIL |
| C — deterministic cascade | 0.5837 | 35.00% | 77.50% | 62.50% | 0 | 2 | FAIL |

Cascade C escalated 68.33% of the complete corpus, far above the intended minority path, while still missing two critical events.

## Verdict

**Recommended routing: `NONE`.**

No strategy satisfied the hard gates. Quality correctly took priority over nominal cost, and no Shadow Feed or later phase was authorized from this benchmark.

## Post-mortem

The benchmark exposed a structural problem: asking the model to combine factual verification, provenance handling, critical-policy enforcement, personal relevance, and attention ranking made errors difficult to localize and allowed high-impact misses.

The cascade reduced critical misses only slightly while adding complexity and failing its escalation target. A better prompt alone could not make factual authority auditable enough.

## Design consequence

Gate 1 led directly to the authoritative Factual Gate used by the MVP:

1. deterministic collection and normalization;
2. explicit provenance and evidence access;
3. locked facts and contradiction status;
4. deterministic `BLOCK`, `REVIEW`, `HOLD`, `ELIGIBLE`, or `MUST_SHOW` action;
5. model limited to attention value only;
6. deterministic ranking and Card construction.

The model can no longer invent or rewrite facts, reinterpret invalid provenance, or downgrade mandatory policy.
