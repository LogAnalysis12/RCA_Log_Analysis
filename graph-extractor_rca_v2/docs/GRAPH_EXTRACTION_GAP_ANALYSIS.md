# Graph Extraction Gap Analysis: RCA Extractor v2

## Scope and verified baseline

This report describes the supplied extractor source and the additive RCA evidence implementation in this package. It does not describe the separate Neo4j importer or LLM traversal pipeline, which were not present in the uploaded `graph-extractor.zip`.

**Important distinction:** the `Span` node was a downstream Neo4j traversal experiment. It is not part of the extractor; this package does not define or create `Span` records, connect to Neo4j, import data, or modify LLM traversal.

The existing Step 3 source extractor and original resumable runner are retained unchanged. The new v2 worker calls the same legacy functions/call/field extraction logic and adds independent RCA artifacts. The existing output contracts are checked against the original merger in `tests/test_extractor_contract.py`.

| Baseline artifact | Treatment in v2 |
|---|---|
| `functions.jsonl` | Preserved by delegation; it still contains the legacy source excerpt field. A future loader must not store that field in Neo4j. |
| `calls.csv` | Preserved. New per-call-site evidence goes to `call_sites.csv`. |
| `data_fields.csv` | Preserved. New variables/parameters/field use evidence is additive. |
| `data_access.csv` | Preserved. New read/write classification goes to `variable_accesses.csv`. |
| `translation_units.csv` | Header preserved; new artifact counts are in `extraction_report.json`. |

## Additions in the extractor design

| RCA evidence family | Artifacts | Why it matters for causal traversal | Evidence boundary |
|---|---|---|---|
| Call-site identity and target resolution | `call_sites.csv`, `external_symbols.jsonl`, `callable_targets.jsonl`, `callable_calls.csv` | Distinguishes multiple calls to the same callee; keeps external and lambda targets visible. | Virtual dispatch, unresolved callbacks and some function-pointer targets can remain unresolved. |
| Variable declaration, parameter and type identity | `variables.csv`, `function_parameters.csv`, `types.csv`, `type_relationships.csv`, `function_return_types.csv` | Supports tracing a value across parameters/returns and distinguishing raw pointers, pointer-to-pointer types, references, arrays, aliases, function pointers and smart-pointer families. | Type shape does not prove ownership, lifetime, alias identity or safety. |
| Read/write and value propagation | `variable_accesses.csv`, `call_arguments.csv`, `data_flows.csv`, `return_flows.csv` | Helps connect mutation sites to readers and follow parameters, arguments, returns, assignments, lambda references and thread-entry arguments. | Flow edges are syntax/position evidence, not complete interprocedural points-to or symbolic execution. |
| Memory and lifetime operations | `lifetime_events.csv`, `pointer_events.csv` | Captures `new`/`delete`, free-family calls, reset/release/clear/erase, moves, smart-pointer calls, dereferences, address-of, pointer/index access, nulling, pointer aliasing and pointer arithmetic where recognized. | Recognizing an operation does not prove a use-after-free, leak, double delete or runtime path. Custom wrappers need configuration or later analysis. |
| Async/thread context | `thread_spawns.csv`, `data_flows.csv` | Finds configured `std::thread`, `std::jthread`, `std::async`, POSIX/Windows thread APIs, wrapper API and generic pool-dispatch patterns; maps arguments to known callable parameters where possible. | Config matching can false-positive. Dynamic dispatch and custom wrappers need review and tailored rules. |
| Locking and synchronization | `lock_usage.csv`, `synchronization_events.csv` | Records RAII lock modes, explicit configured lock calls, join/detach, wait/notify, and atomic-like operations. | A lexical lock scope is not proof that a particular access is protected; atomic operations do not prove a compound invariant is correct. |
| Control flow and macro provenance | `control_flow_events.csv`, `macro_events.csv` | Captures null/presence checks, throw/catch, empty/null return, and detailed preprocessing-record macro observations. | Macro-to-event association on the same source line is explicitly heuristic; Clang source mapping can vary. |
| External API semantics | `thread_api_config.json` | Allows project-specific thread APIs, lock primitives, synchronization calls, and smart-pointer spelling patterns to be configured without changing traversal code. | This is a manually maintained semantics inventory, not a complete platform/API database. |

## Existing gaps and known limits

1. **AST end-to-end validation has not yet been run in the current Windows/container session.** This environment does not have the Python `clang.cindex` binding installed. `py_compile` and contract tests pass, but neither is a substitute for parsing the real repository with its actual `compile_commands.json` and a compatible LibClang build.
2. **Build coverage controls source coverage.** The runner processes C/C++ translation units selected from `compile_commands.json`. A source file missing from the compile database will not be parsed merely because it exists in the repository. Headers are usually parsed through the translation units that include them; headers not included by any selected translation unit may lack full semantic coverage.
3. **C and C++ only.** Java, C#, JavaScript/TypeScript, Rust, Python, and other adapters are not implemented. `MULTI_LANGUAGE_ADAPTER_CONTRACT.md` is a future design boundary, not a claim of multi-language support.
4. **Pointer/alias analysis is not whole-program points-to analysis.** Raw pointer depth, structural pointee types, common local aliases, assignments and argument flows are evidence. Interprocedural aliases through containers, fields, factories, callbacks, casts, reflection-like mechanisms, opaque libraries and custom ownership wrappers may remain incomplete.
5. **Smart-pointer labels are type/operation clues, not correctness proofs.** Aliases are checked using canonical type spelling, but custom template wrappers or unusual typedef chains can still require config/tuning. The extractor does not establish whether concurrent access to a smart-pointer *variable* is data-race-free.
6. **Concurrency semantics are only partially static.** Spawn APIs and known synchronization calls are discoverable only when configured or recognized. The extractor does not prove happens-before ordering, whether two threads execute concurrently, whether an atomic check-then-act is logically safe, or whether a lock actually protects every relevant memory access.
7. **Macros/templates/platform APIs need fixture-driven checking.** Macro records rely on detailed preprocessing information; expansion association is heuristic. API overloads and wrappers vary by project and platform.
8. **No root-cause labels enter the graph.** Ground truth is read by a separate checker only after extraction. The extractor emits evidence records and uncertainty, not `culprit`, `victim`, `root cause`, or a Span record.
9. **Neo4j integration remains separate.** The uploaded package did not include the current Neo4j importer or traversal code. This deliverable only defines extraction artifacts and their proposed relationships. Do not import the new schema until the importer is reviewed against the actual target database and the live extractor output.

## Current checks completed here

- Python syntax compilation of the new runner, worker, extractor, ground-truth checker and wrapper.
- Contract tests comparing legacy artifact headers from the new merge path with headers from the preserved legacy merger.
- Contract checks for all additive artifact headers and schema declarations.
- Configuration validation for configured thread/lock/synchronization patterns.
- `NO_LOCK` checker unit test proving the negative condition is reported as an expected-absence check, not as a positive lock row.
- Dry-run source-discovery tests proving compile-database selection can be checked without parsing ASTs or writing extraction output.
- Canonical-spelling regression test for smart-pointer typedef/using aliases.
- Artifact-integrity validator contract test over an empty but schema-complete extraction bundle.
- **Current test result: 10 passed.**

Not completed here: live LibClang extraction, S1-S6 ground-truth result from live AST records, full repository extraction, graph import, and Neo4j traversal. These require the office Linux environment and current project compile database.

## Acceptance gates for the Linux run

1. Verify the venv can import `clang.cindex` and matches the shared LibClang 21 library.
2. Inspect the compile database and run `--dry-run` first. Review the selected source list and ensure the repository root matches the compile commands.
3. Run `--limit 1` into a **new scratch output directory**, inspect the checkpoint and all evidence artifacts, and check that source locations and targets are plausible.
4. Run the ThreadDemo ground-truth checker against the existing S1-S6 `ground_truth.json`. Investigate each missing edge against its AST/source before changing rules; never tune the extractor blindly to match expected answers.
5. Run the full ThreadDemo extraction into a separate fresh scratch path, then rerun validation. Mark any limited/failed TU run as partial and do not load it as a complete graph.
6. For the next system/Neo4j update, review and version the importer separately. Keep `Span` as an independent loader-side traversal experiment, not as an extractor artifact.
