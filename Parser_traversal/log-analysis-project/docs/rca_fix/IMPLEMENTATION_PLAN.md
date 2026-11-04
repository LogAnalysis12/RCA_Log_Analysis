# Investigator Frontier Implementation Plan

## Constraints

- Preserve existing uncommitted work. Work on `fix/investigator-frontier`.
- Do not rewrite the system or change the Neo4j schema, extraction pipeline,
  Gemini integration, or token-budget settings.
- Use deterministic offline tests with a fake LLM and in-memory graph. Do not
  call Gemini or modify Neo4j.
- Stop after each phase and report changed files, decisions, tests, failures,
  and requirements still open. Wait for review before starting the next phase.

## P1: Source Cache Integrity

Status: implemented; awaiting review.

- Record the exact matched source hash variant and a whole-file snapshot
  identity in verified source results.
- Replay only `SOURCE_VERIFIED` entries whose matched digest, expected digest,
  variant, and snapshot identity remain present and consistent.
- Keep verified source evidence reusable. Preserve source text and verification
  metadata on replay and through non-mutating compaction.
- On cache-integrity failure, perform one system-level source reread before
  repeated-action handling. Do not ask the Investigator to retry it. Bound the
  automatic reread to one attempt per request key.
- Keep the final-newline regression active, not xfailed.

## P2: Causal Frontier and Deterministic S7 Dispatch

Status: implemented; awaiting review.

- Store explicit, deduplicated investigation questions with dependencies,
  status, evidence references, and priority.
- Only the orchestrator may close or reopen a question. Tool execution alone
  never closes one.
- Count progress only when a question is validly closed or genuinely new
  evidence is added; opening a question alone is not progress.
- Make the mandatory S7 investigation deterministic: discover `runS7` through
  `find_worker_launchers(runT2)`, inspect it, call
  `find_spawned_workers(runS7)`, enumerate every spawned worker, and read the
  required sibling worker sources.
- Investigate T6, its forwarding path, and `releaseCurrent`; trace writers of
  `released`, `current`, and `readyCount`; verify synchronization and
  resource-release ordering.
- Enforce a sibling-worker gate in controller dispatch. Do not allow unrelated
  caller traversal until the required sibling investigation is satisfied.

## P3: Typed Outcomes and Bounded Recovery

Status: not started.

- Distinguish nonempty results, definitive empty results, not-applicable empty
  results, transient errors, permanent errors, and cache-integrity errors.
- Never call an empty graph result definitive while graph coverage or
  applicability is unknown.
- Redirect repeated actions to the highest-priority unresolved frontier
  question. Do not ask Gemini to choose an alternative when a mandatory task
  is known.
- Replace `STALLED` only alongside bounded frontier-exhaustion and recovery
  statuses. Keep retries and recovery finite.

## P4: Ordering Verifier

Status: not started.

- Validate source-backed evidence, exact citations, relevant field accesses,
  worker counts, synchronization, and the causal relation between release and
  observation.
- Never infer happens-before from line numbers across functions. Require the
  verified mutex/condition-variable protocol and relevant thread execution
  paths.
- Reject graph-only ordering claims and unsupported or inexact citations.

## P5: Independent Critic

Status: not started.

- Keep the Critic independent of the Investigator narrative.
- Let the Critic issue structured evidence requests. The orchestrator validates
  each request, executes permitted reads, and returns verified evidence to the
  Critic before it decides.
- Keep verdicts grounded in the same source, citation, synchronization, and
  causal checks required by the deterministic verifier.

## P6: Deterministic S7 End-to-End Regression

Status: not started.

- Build the regression around a fake LLM and in-memory graph, with source
  fixtures and no Gemini calls or Neo4j modifications.
- Cover launcher discovery; spawned-worker enumeration; sibling-worker source
  inspection; the `releaseCurrent` caller chain; LF-final-newline cache replay;
  repeated-action recovery; ordering verification; and Critic acceptance and
  rejection.
- Include the required S7 investigation gate and prove unrelated caller
  traversal cannot run before it is satisfied.

## Pre-P1 Cache Test Reconciliation

The P0 report described two failures caused by tests using a stale three-argument
`_cached_source_replay` call. That description does not match the current dirty
worktree: its modified cache test uses the current two-argument API. Before this
phase, the focused offline slice collected 24 passing tests and one xfailed
LF-final-newline regression. The xfail represented a real cache-contract bug,
not an API mismatch; it has been converted to a normal passing regression by
P1. No test was weakened to obtain a green result.