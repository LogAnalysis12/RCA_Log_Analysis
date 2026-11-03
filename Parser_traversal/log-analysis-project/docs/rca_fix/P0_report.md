# P0 Report: Investigator Loop Inspection

## Scope and state

This phase was inspection only. No runtime code, prompts, tools, graph data, or tests were changed. The required work branch is `fix/investigator-frontier`. The repository already had many uncommitted changes before this phase; they were preserved.

The report describes the current working-tree versions, not a clean `main` baseline. In particular, the working tree has modified `investigator.py`-adjacent test/cache surfaces, and two cache replay tests do not match the current helper signature.

## 1. Main loop, step by step

1. `run_investigation()` selects the resolved anchor, initializes `InvestigationState`, optional function memory and ledger, then creates the Investigator backend with the full `TOOLS` list (`traversal/investigator.py:95-142`).
2. The controller sends an initial JSON message telling the model to read the anchor and trace relevant callers, callees, or field writers (`traversal/investigator.py:144-163`).
3. For each of up to `max_steps`, a response without tool calls enters no-tool recovery; the controller may return `STALLED` after `stall_retries` (`traversal/investigator.py:166-190`).
4. A tool-call turn is fingerprinted. Each ordinary call has decision metadata removed, is recorded in the ledger as planned, and is keyed by tool name plus normalized arguments (`traversal/investigator.py:191-257`).
5. The controller applies the no-progress gate, repeat guard, and verified-source cache logic, or executes the tool via `_execute_tool()` (`traversal/investigator.py:258-325`, `traversal/investigator.py:593-610`).
6. It fingerprints meaningful evidence, appends the full result to in-memory tool history, finishes the ledger event, compacts the result for the model, and builds a context checkpoint (`traversal/investigator.py:327-446`).
7. If no conclusion was proposed, it resets/increments `no_progress_steps`, adds recovery guidance, and either returns `STALLED` or sends the tool results back to the same Investigator (`traversal/investigator.py:460-503`).
8. If a conclusion is proposed, the controller checks duplicate conclusions, runs `verify_conclusion()`, and, if that passes, calls `run_critic()`. An accepted Critic returns `ACCEPTED`; a rejected Critic is fed back to the Investigator (`traversal/investigator.py:505-583`).
9. Exhausting `max_steps` returns `MAX_STEPS_REACHED`; budget and unhandled errors return `BUDGET_EXCEEDED` and `INVESTIGATION_FAILED` (`traversal/investigator.py:585-590`).

## 2. Progress and `no_progress_steps`

A tool call is progress only when `_meaningful_evidence_fingerprint()` returns a new fingerprint. A new hash-verified source read counts; a cache hit, failed/unverified source, or repeated evidence does not. Non-empty graph relationships count; an empty graph result counts only when the model attached a specific `question_to_resolve` (`traversal/investigator.py:916-958`).

At the end of a tool turn without a conclusion, any new evidence resets the counter to zero; otherwise it increments by one. The controller sends recovery guidance when the configured prompt threshold is reached or the tool-turn fingerprint repeats. At `no_progress_limit`, it returns `STALLED` even if unanswered causal work remains (`traversal/investigator.py:460-503`). A no-tool response uses a separate `stalls` counter and can also return `STALLED` (`traversal/investigator.py:166-190`).

The S7 failure is consistent with this design: after the worker and launcher relationships were discovered, the model was still responsible for selecting the next action. Repeating an already-known `runT2` request generated no new evidence; prompts asked it to choose another action, but no controller state represented the unanswered `ENUM_WORKERS(runS7)` task or executed it.

## 3. Repeated-action guard

`repeats` is a `Counter` keyed by normalized tool name and JSON-sorted arguments. For each tool call, the controller increments the count and checks:

- after enough unproductive turns, a missing `decision.question_to_resolve` blocks the action;
- after enough unproductive turns, a repeated equivalent action without `backtrack_reason` is blocked;
- cached source replay is blocked after `repeated_tool_limit`;
- any other identical action is blocked after `repeated_tool_limit`.

The blocked result is a `TOOL_ERROR` dict with an explanation and continuation instruction. It does not contain a memoized prior result or a list of concrete top pending tasks. The tool result is marked `BLOCKED`, so it cannot count as new evidence (`traversal/investigator.py:255-315`, `340-385`). The guard limits loops but does not redirect to an executable next question.

## 4. Source cache: storage, verification, replay, and compaction

**Storage.** A verified `read_function_source` result is stored in the per-run `verified_source_cache` under the exact tool/argument key. The same result is also placed in `state.read_functions` keyed by canonical function id and optionally passed to `FunctionUnderstandingMemory.remember_source()` (`traversal/investigator.py:132-135`, `314-325`). The replay cache is in-memory and has no file snapshot identity or git blob metadata.

**Initial source read.** `NeptuneTraversalTools.read_function_source()` obtains function metadata and calls `read_source_range()`. It returns `FUNCTION_NOT_FOUND`, `SOURCE_UNAVAILABLE`, `SOURCE_READ_FAILED`, `SOURCE_VERIFIED`, `SOURCE_HASH_MISMATCH`, or `SOURCE_READ_UNVERIFIED` depending on metadata, read errors, and hash result (`traversal/neptune_tools.py:92-131`). Hash verification computes LF/CRLF variants, each with and without a final newline, and `content_hash_matches` becomes true if any variant matches (`traversal/source_reader.py:88-105`, `196-225`). But `SourceReadResult.computed_content_hash` is always set to the LF-without-final-newline digest (`traversal/source_reader.py:241-247`).

**Exact LF_FINAL_NEWLINE failure.** An expected hash can match `LF_FINAL_NEWLINE`, making `content_hash_matches=True` and the result status `SOURCE_VERIFIED`. The result still has `computed_content_hash=hash_variants["LF_NO_FINAL_NEWLINE"]`. On a repeated request, `_cached_source_replay()` requires both `content_hash_matches is True` and `expected_content_hash == computed_content_hash` (`traversal/investigator.py:38-55`). These conditions conflict for a valid final-newline-only match. Replay returns `None`; the caller converts that to the exact error `Cached source failed verification; reread required.` (`traversal/investigator.py:294-298`). The repeat guard can then block later requests to the same key (`traversal/investigator.py:279-293`). This is the precise code path behind the reported message.

**Compaction.** For the model, `compact_tool_result()` creates a new dict, truncates a copied string for display, and carries through hashes and `hash_match_variant`; it does not mutate `verified_source_cache` (`traversal/compact_results.py:38-57`, `73-102`). Error dicts are shaped before tool-specific handling (`traversal/compact_results.py:50-59`). There is no source snapshot identity check, and the current cache contains the original result, not a read-only cache abstraction with a version key.

## 5. Current tool result behavior for empty and error cases

Graph tool methods call `_execute()`, which directly returns `graph.query()` results and does not catch graph exceptions or convert them into a structured error (`traversal/neptune_tools.py:58-59`). The controller also does not wrap `_execute_tool()` in a per-tool exception handler; an exception reaches the outer handler and ends the investigation as `INVESTIGATION_FAILED` (`traversal/investigator.py:314-315`, `585-590`).

- `find_callers`: valid empty result is `status: OK`, count zero, and empty caller arrays (`traversal/neptune_tools.py:159-200`).
- `find_callees`: valid empty project/external results are `status: OK` and empty arrays; external boundary is false (`traversal/neptune_tools.py:202-286`).
- `find_spawned_workers`: valid empty result is `status: OK`, worker count zero, and empty workers (`traversal/neptune_tools.py:298-369`). There is no check for “target is a worker”; an empty result cannot distinguish not-applicable from no known children.
- `find_worker_launchers`: valid empty result is `status: OK`, launcher count zero, and empty launchers (`traversal/neptune_tools.py:372-451`).
- `list_fields_read` / `find_fields_read`: valid empty result is `status: OK` with zero field accesses and empty arrays (`traversal/neptune_tools.py:453-506`).
- `find_field_writers`: invalid/blank or unresolved identifiers return `FIELD_NOT_FOUND`; ambiguous names return `AMBIGUOUS_FIELD_NAME`; valid query with no writers returns `status: OK`, writer count zero, and empty writers (`traversal/neptune_tools.py:508-641`).
- `read_function_source`: structured not-found, unavailable, read-failed, hash-mismatch, and unverified outcomes as described above; graph exceptions are not normalized into these statuses (`traversal/neptune_tools.py:92-131`).
- `_execute_tool()`: unknown tool name returns `{"error": "Unknown tool: ..."}` (`traversal/investigator.py:593-610`).

There is no typed distinction matching `OK_EMPTY_DEFINITIVE`, `OK_EMPTY_NOT_APPLICABLE`, `TRANSIENT_ERROR`, `PERMANENT_ERROR`, and `CACHE_INTEGRITY`. Empty arrays alone are not enough to determine whether a query is inapplicable or definitive.

## 6. STALLED and recovery decisions

`STALLED` is returned from two places in `run_investigation()`: repeated no-tool responses beyond `stall_retries` (`traversal/investigator.py:166-190`) and no-new-evidence tool turns at `no_progress_limit` (`traversal/investigator.py:485-503`). The CLI still treats `STALLED` as a non-success status (`traversal/run_traversal.py:108-115`). Recovery is prompt-driven: `_no_tool_recovery_prompt()` and `_no_progress_recovery_prompt()` provide a checkpoint and ask the model for another action (`traversal/investigator.py:747-785`, `987-1018`). No orchestrator action is dispatched automatically.

## 7. A-J design coverage and plug-in points

- **A Frontier:** Not implemented. `InvestigationState` has read functions, tool history, evidence, usage, and Critic feedback only (`traversal/schemas.py:25-39`). A frontier can be stored in this state and made durable by extending `InvestigationLedger` (`traversal/investigation_ledger.py:23-39`, `58-122`). Triggering/closure logic belongs in a dedicated frontier module called after successful tool results are recorded.
- **B Progress:** Partly present: evidence fingerprints and no-progress counting exist (`traversal/investigator.py:916-958`, `460-468`). It does not count a frontier question closed or a new question opened because no frontier exists.
- **C Phases/auto-dispatch:** Not implemented. The controller is a single `for step_number` loop with terminal `STALLED` branches (`traversal/investigator.py:164-190`, `485-503`). Phase selection and mandatory-question dispatch would plug into `run_investigation()` before calling the Investigator backend.
- **D Source cache:** Partly present: per-run result cache and hash variants exist (`traversal/investigator.py:38-61`, `source_reader.py:88-105`). The cache does not store snapshot identity; the replay hash condition is incorrect for valid variants. One-time automatic reread and integrity question closure are absent.
- **E Typed outcomes:** Not implemented. Tools return untyped dicts; valid empty graph responses all use `OK`. Add a typed result contract at the `NeptuneTraversalTools`/controller boundary, with stable mapping into model-facing JSON.
- **F Redirect repeated guard:** Partial guard exists, but blocked results are generic errors. It does not return the memoized result tagged as already answered or current pending questions (`traversal/investigator.py:279-313`). Replace this branch at the controller boundary, keeping redirected attempts as no progress.
- **G Sibling gate:** Not implemented. All tools remain in the initial Investigator tool list (`traversal/investigator.py:158-164`; `traversal/tool_definitions.py:1-97`). Restrict the per-turn allowed tool schema and enforce it again in controller dispatch while sibling questions remain open.
- **H Ordering verifier:** Partial deterministic verifier checks source status, file and line range, required tool routes, external boundaries, and some parameter/state-mutator causality (`traversal/verifier.py:249-311`). It does not validate verbatim cited statements, field synchronization evidence, statement order, or worker-count thresholds. It also does not require structured quote citations in the conclusion schema (`traversal/tool_definitions.py:65-97`). Add ordering checks to the verifier, using verified `state.read_functions` and scoped graph evidence; graph relationships alone must not pass.
- **I Critic:** Partly independent but not as designed. `run_critic()` creates a separate backend with no tools and passes proposed conclusion, deterministic verification, packaged verified evidence, and a summary of tool/input pairs (`traversal/critic.py:16-48`). It cannot reread sources or reopen frontier questions because it has no tools and the state has no frontier. This is a design/interface conflict to resolve before P5: decide whether the Critic gets a restricted source-reading interface or the orchestrator performs its requested rereads on the Critic’s behalf. It should still receive no Investigator narrative.
- **J Prompt:** Not implemented. The system prompt currently contains long procedural steps and concurrency/causal-chain checklists (`traversal/prompts.py:1-98`). The Critic prompt is separate (`traversal/prompts.py:99-117`). Once orchestration owns work selection, reduce the Investigator prompt to evidence interpretation and open questions; retain source/provenance constraints in deterministic code and schemas.

## 8. Proposed file-by-file changes and risks

- `traversal/frontier.py` (new): define question types, statuses, dedup key, deterministic trigger rules, evidence-based closure, and selection of the next mandatory question.
- `traversal/investigation_ledger.py`: persist frontier open/close/reopen events and enough IDs/arguments to reconstruct current questions. Keep source text out of the append-only ledger.
- `traversal/schemas.py`: add frontier and phase state to `InvestigationState` serialization.
- `traversal/investigator.py`: add phase progression, frontier triggers, mandatory auto-dispatch, per-turn sibling gate, redirect-style repeat results, one-time source-integrity reread, and removal of `STALLED` terminal outcomes. Keep actual LLM tool execution bounded by existing token/call budgets.
- `traversal/neptune_tools.py`: introduce typed internal outcome distinctions, classify `find_spawned_workers(worker)` as not applicable only when there is affirmative launcher evidence, and add snapshot identity metadata to verified source results/cache entries. Avoid changing graph schema or extraction.
- `traversal/source_reader.py`: preserve the matched hash/variant correctly and expose stable file identity (or accept an identity provider); a size+mtime identity can collide or change during reads, while a git blob ID requires repository metadata and careful worktree semantics.
- `traversal/compact_results.py`: compact a copy/read-only view and preserve typed outcome, cache identity, verified hash, and citation provenance.
- `traversal/tool_definitions.py`: validate proposed extra questions against known function IDs; enforce structured cited claims for conclusions; expose only allowed tools for each gated turn.
- `traversal/prompts.py`: remove procedural routing checklist and tell the model to interpret current evidence and answer displayed open questions.
- `traversal/verifier.py` (and likely new `traversal/ordering_verifier.py`): validate source-backed quotes, cited source ranges, field evidence, order of state changes, and enumerated observer threshold. Reject graph-only claims.
- `traversal/critic.py`: provide claim/citations/ledger summary and implement an independent restricted reread/reopen path. Resolve the interface conflict above before coding this stage.
- `traversal/run_traversal.py` and report/status tests: update status mapping after `STALLED` is removed; preserve explicit inconclusive/failure statuses.
- Tests: create an in-memory S7 graph, fake scripted/random backends, source snapshot fixtures, and socket-denial tests. Cover phase transitions and each safety gate without network or Neo4j.

Main risks: changing terminal status may affect CLI exit/report behavior; frontier persistence must recover correctly after partial writes; auto-dispatch must obey existing attempt/token budgets; source identity must mean the same snapshot used for hash verification; typed outcomes must preserve legacy model-visible result fields; Critic rereads must be independent without allowing its verdict to rely on graph-only evidence; and sibling gating must be enforced in code, not just tool descriptions.

## Errors and tests

No implementation errors were encountered because P0 made no runtime edits.

Offline inspection tests run:

```text
python -m pytest -q traversal/test_investigator_progress.py traversal/test_investigator_recovery.py traversal/test_cached_source_replay.py traversal/test_tools.py traversal/test_worker_launchers.py traversal/test_verifier_causality.py

22 passed, 2 failed
```

Both failures are in the pre-existing working-tree `test_cached_source_replay.py`: those tests call `_cached_source_replay(key, cached, replayed)`, but the current helper accepts only `(key, cached_source)`. This is a test/code signature mismatch, not a failure produced by a P0 code change. I did not edit the tests or helper.

The test command used fake/unit fixtures only; no Gemini or graph/network investigation was started. Test coverage for the requested S7 loop reproduction, final ordering rules, socket denial, and the complete A-J design is not present yet.

## Uncertainties / stop point

The P0 code-path findings are specific to the current dirty working tree. Before P1, reconcile the stale cache test call signature with current `investigator.py`, and decide how the Critic can independently reread verified sources and reopen questions while remaining isolated from Investigator narrative. Per the phase gate, work stops here; no P1 tests or implementation have been started.
