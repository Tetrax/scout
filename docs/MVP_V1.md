# Scout MVP V1 — sanitized scope

## Product hypothesis

With a small set of reliable sources, an authoritative fact gate, attention-only model triage, and at most three Cards per manual run, Scout can provide useful discoveries at an acceptable attention cost.

The MVP validates user value, not final source coverage, deployment architecture, or model cost.

## Invariants

- One configurable local Profile per run.
- Manual invocation only; no cron, daemon, or silent automation.
- JSON/JSONL storage only, outside the source checkout.
- `gpt-5.6-sol` is a provisional quality choice for validating user value, not the final cost choice.
- No model cascade.
- At most three Cards per Run; zero is valid.
- Cards remain local in the current implementation.
- Feedback never rewrites a Profile automatically.

## Current source scope

The implemented vertical slice collects only official releases from `NousResearch/hermes-agent` through the fixed GitHub releases API route. General GitHub activity, discovery, social sources, security advisories, Reddit, and the general Web are out of scope.

## Authoritative facts

`provenance_status`, `evidence_access`, `freshness`, `material_change`, `critical_policy`, `contradiction_status`, `gate_action`, and `locked_facts` are not editable by the model.

`UNKNOWN` never means false. Invalid provenance is blocked. A `MUST_SHOW` gate cannot be downgraded.

## Attention triage

The model may return only:

- `SHOW`, `KEEP_INTERNAL`, or `REJECT`;
- `DIRECT`, `ADJACENT`, or `WEAK` thematic fit;
- `HIGH`, `MEDIUM`, or `LOW` materiality;
- `NOW`, `LATER`, or `NONE` attention value;
- one bounded reason code and short rationale.

## Ranking

Priority is deterministic: mandatory items, immediate attention, materiality, thematic fit, bounded adjacent serendipity, then freshness. An adjacent item may replace only a non-mandatory selected item. More than three mandatory items fails closed.

## Explicitly deferred

- automatic Profile learning;
- automatic delivery and feedback ingestion;
- model cascades and cost optimization;
- additional sources or broad discovery;
- schedulers, agents, APIs, Web Apps, databases, and multi-profile services.
