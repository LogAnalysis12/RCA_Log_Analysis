# Graph Extraction Gap Analysis: S7-S13 (snapshot audit, pending P3 rerun)

## Status and scope

This is a source/schema audit of the exact `graph-extractor.zip` provided with this ThreadDemo upload. It is **not** a final report of P3 extraction results. The ZIP does not contain the spawn-aware P3 files previously described, and the current validation container lacks the `clang` Python bindings required to execute this version. No P3 output was overwritten or reused.

## Observed extractor snapshot

The included `extract_graph_step3_fields_working.py` and `README_graph_extractor.md` describe these artifacts:

- `functions.jsonl`
- `calls.csv`
- `data_fields.csv`
- `data_access.csv`
- `translation_units.csv`
- `extraction_report.json`
- `manifest.json`

Their documented schema covers function definitions, call occurrences, persistent fields, and field accesses. The supplied ZIP does not contain a named `thread_spawns.csv`, `lifetime_events.csv`, `lock_usage.csv`, or a documented `SPAWNS`/thread-spawn edge schema. This is an archive-content finding, not a claim that a different local P3 version lacks those features.

## Source requirements visible in S7-S13

| Scenario | Source construct / evidence to recover | Why a normal call graph may be insufficient |
|---|---|---|
| S7 | Eight `std::thread` member-function entries; T6 -> `CS7Transport::forward` -> `CS7ResourceManager::releaseCurrent`; `unique_ptr::reset`; condition-variable ordering | The mutating function is two calls below a spawned entry, and the pass-through must not be labelled as the writer. |
| S8 | `std::thread`, `shared_ptr::use_count()`, `weak_ptr::lock()`, ownership count changing after the check | The ownership assumption depends on ordering and pointer semantics, not just caller/callee edges. |
| S9 | `std::move(owner)`, `unique_ptr::release()`, immediate re-wrap | Ownership transfer is encoded through arguments and values, not a direct ordinary call edge alone. |
| S10 | `shared_ptr::reset()`, `weak_ptr::lock()`, gated release-before-consume order | The key result is expired weak ownership, which must not be reported as an actual raw-pointer use-after-free. |
| S11 | Macro invocation `RELEASE_AND_CLEAR(state.handle)` plus macro definition expanding to `.reset()` | Extraction needs macro expansion/definition evidence; a call-site-only representation hides the mutation. |
| S12 | Lambda reference capture of `localValue`, callback stored in `std::function`, deferred invocation on a worker thread, lifetime-token guard | The capture/escape/lifetime relation is not fully represented by a simple function-call edge. |
| S13 | Two separately locked functions each check `available > 0`, then `fetch_sub(1)` on the same atomic | Atomic read-modify-write operations can be race-free while a check-then-act invariant still fails. |

## Required next extraction run

When the current spawn-aware P3 sources are provided, run their repository extractor on `ThreadDemo/src` into a **new scratch output directory**. Record the exact output counts, edge rows and missing relationships for the cases above. Do not overwrite `out/threaddemo_raw_p3` or any earlier baseline. Specifically verify:

1. `runS7` has spawn relationships to all eight worker entry points.
2. `CS7Workers::runT6` connects to the non-mutating `CS7Transport::forward`, then to the mutating `CS7ResourceManager::releaseCurrent`.
3. Thread, async, thread-pool, native-thread, lock, lifetime and macro behavior are supported based on actual P3 output, not inferred from log text.
4. The `RELEASE_AND_CLEAR` invocation can be linked to the macro definition and its `reset()` expansion.
5. The graph importer maps the extractor's actual output schema without dropping spawn, lifetime, or lock relationships.

## Files missing from the supplied graph archive

The archive did not include `EXTRACTOR_CHANGES.md`, `P3_match_rules.md`, the spawn-aware extractor version, graph loader/importer/schema, or the RCA `traversal/` package. Those files are needed before making a safe extractor or Neo4j loader change. This report should be updated with measured scratch extraction results rather than treating the source inventory above as proof of a successful graph extraction.
