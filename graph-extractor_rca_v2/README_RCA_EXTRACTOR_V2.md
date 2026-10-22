# RCA Graph Extractor v2

## Purpose and boundary

This package extracts **source-level evidence** from a C/C++ repository using Clang/LibClang and the repository's `compile_commands.json`. Its goal is to provide a better evidence graph for root-cause analysis: call sites, value/alias flow, object lifetime, pointer operations, thread entry points, synchronization, types, and source locations.

The extractor does **not** decide that a line is the root cause. It records AST-backed facts and explicitly marked heuristics so a later graph traversal and the RCA pipeline can compare them with logs and determine a plausible causal chain. Static extraction does not prove runtime ordering, that a branch executes, that a data race occurs, that a mutex protects a particular access, or that all aliases are known.

**Important distinction:** the `Span` node was an experiment made in the Neo4j graph to test LLM traversal. This extractor does not create, load, or depend on `Span` nodes. No Neo4j writes or traversal changes are included here.

## Design rules

1. **Preserve the legacy output contract.** `functions.jsonl`, `calls.csv`, `data_fields.csv`, `data_access.csv`, and `translation_units.csv` keep their existing names and column/record contract. The implementation delegates legacy functions/call/field extraction to the existing `extract_graph_step3_fields_working.py`; that module is not rewritten here.
2. **Add, do not repurpose.** RCA-specific evidence is in new artifacts. Existing `calls.csv` is not overloaded with thread/lifetime/type semantics.
3. **Stable IDs and call-site identity.** IDs are deterministic from repository/source identity or Clang USRs where available. Two call sites to the same callee remain distinct in `call_sites.csv`.
4. **Source-relative locations.** New code-evidence records use `relative_path`, line, and column where Clang provides them. External symbol declarations retain `declaration_file` because those declarations may be outside the repository.
5. **Evidence grading.** Fields named `evidence_grade` indicate whether a record comes from a direct AST/type API, configuration rule, or heuristic. A heuristic should not be treated as proof.
6. **Configuration for platform APIs.** Thread creation APIs, lock APIs, synchronization APIs, and smart-pointer spelling patterns live in `thread_api_config.json`, rather than being buried in the extraction traversal.
7. **Ground truth is a separate validation step.** The extractor never opens `ground_truth.json`. `tools/check_graph_vs_truth.py` reads ground truth only after extraction and writes a separate check report. `NO_LOCK` is validated as an expected absence, not emitted as an ordinary positive lock edge.
8. **Incremental and resumable.** Each translation unit has an isolated worker/checkpoint. A partial run is marked in `extraction_report.json` and `manifest.json`; do not treat a partial run as repository-complete.

## What is extracted

### Legacy artifacts, unchanged

| Artifact | Role |
|---|---|
| `functions.jsonl` | Existing function definitions and legacy fields, including the legacy source excerpt property. |
| `calls.csv` | Existing direct call records and resolution metadata. |
| `data_fields.csv` | Existing field declarations. |
| `data_access.csv` | Existing field access records. |
| `translation_units.csv` | Existing per-TU status and diagnostics summary. |

The **future Neo4j loader must not store the legacy `source_code`/source excerpt property** as a node property. It can contain comments as well as code, and it is not required for graph traversal. Source should be fetched through the controlled source-reading boundary used by the RCA pipeline. Do not inject ground-truth answers into extracted graph properties.

### RCA evidence artifacts

| Artifact | Contents and main use |
|---|---|
| `variables.csv` | Project variables and parameters, declaration identity/location, type ID, raw-pointer depth, reference kind, function-pointer flag, smart-pointer family and ownership hint. |
| `function_parameters.csv` | Parameter index, stable parameter/variable ID, and declared type ID. `function_id` and `callable_id` distinguish ordinary function parameters from lambda parameters. |
| `variable_accesses.csv` | `READ`, `WRITE`, `READ_WRITE`, and `POSSIBLE_WRITE` source uses for locals, parameters, fields, and globals. These are syntax-level classifications. `callable_id` is used for lambda-body evidence rather than inventing a Function node. |
| `call_sites.csv` | One record per call/constructor site, separate from the callee. Keeps caller, callee node ID, resolution state, expression, and source location. |
| `call_arguments.csv` | Argument index, argument type, referenced variables, address-of, `std::ref`, `std::move`, `std::forward`, and project parameter mapping when resolvable. |
| `data_flows.csv` | Static argument-to-parameter, assignment/alias, pointer initialization, smart-pointer copy, move, lambda-body references, and thread-argument-to-entry-parameter relations. Every record has a flow kind and evidence grade. |
| `function_return_types.csv` | Function return type ID, spelling, pointer/reference depth, smart-pointer family, and location. |
| `return_flows.csv` | Return expression and referenced values, plus a pointer-like return hint. |
| `types.csv` | Project type declarations and type-use nodes, including built-ins, raw pointer/reference wrappers, arrays, function pointers, and recognized smart-pointer families. |
| `type_relationships.csv` | `INHERITS`, `TYPE_ALIAS_OF`, `POINTS_TO_TYPE`, `REFERENCES_TYPE`, `ELEMENT_TYPE`, `SMART_POINTER_TO_TYPE`, and `WEAKLY_REFERENCES_TYPE` where Clang/type spelling supports them. Raw pointer `POINTS_TO_TYPE` does **not** imply ownership. |
| `lifetime_events.csv` | Explicit `new`/`delete`, `free`-family, `reset`, `release`, `clear`, `erase`, `make_unique`/`make_shared`, move-related and smart-pointer observer/ownership API hints, with target and source location. |
| `pointer_events.csv` | Pointer dereference/address-of/arrow/index, alias initialization/reassignment, pointer arithmetic/nulling, explicit allocation/deallocation and smart-pointer-related sites. |
| `thread_spawns.csv` | API/config rule, spawn kind, separate spawner/target Function, Callable or ExternalSymbol IDs, function-pointer variable if seen, passed variables, target expression, and location. Unresolved wrappers remain explicit rather than fabricated as function targets. |
| `callable_targets.jsonl` | Lambda callable IDs and source locations, plus body calls where observed and a content hash. It does not emit a raw lambda source excerpt. Lambda references are not automatically labelled as safe captures. |
| `external_symbols.jsonl` | External functions/constructors referenced by project code, so calls can terminate at a represented node instead of disappearing. |
| `lock_usage.csv` | RAII lock wrappers and explicit lock/unlock APIs. RAII scope is lexical. `defer_lock`, `try_to_lock`, and `adopt_lock` have distinct event types; try-lock is not considered definite acquisition. |
| `synchronization_events.csv` | `join`/`detach`, condition-variable wait/notify, atomic-like operations and configured synchronization calls. Atomic APIs are evidence of operations, not proof that a logical check-then-act is correct. |
| `control_flow_events.csv` | Explicit null checks, pointer-presence tests, `throw`, `catch`, and empty/null returns. It does not perform whole-program symbolic execution. |
| `macro_events.csv` | Clang detailed preprocessing records for macro definitions/instantiations/expansions where available. Lifetime/pointer events on the same expansion line can carry macro IDs as a **heuristic association**. |
| `type_relationships.csv` | Also records class inheritance; structural pointer/type relations share the same source/target type-ID contract. |
| `ground_truth_check.csv` | Created only by the post-extraction checker, not by the extractor. Per-edge `FOUND`/`MISSING`, with a distinct expected-absence mode for `NO_LOCK`. |

The exact emitted columns are declared in `extract_repository_graph_rca_v2.py` (`EXTRA_CSV_FIELDS`). `rca_graph_schema_v2.json` summarizes the graph-facing semantics. New fields can be added additively in a future schema version; do not silently repurpose existing fields.

## Evidence categories and certainty

Common `evidence_grade` values include:

- `STATIC_AST`: AST node/cursor evidence such as a dereference or explicit delete expression.
- `CLANG_TYPE_API`, `CLANG_TEMPLATE_ARGUMENT_API`, `CLANG_FUNCTION_TYPE_API`: Clang type/declaration APIs.
- `STATIC_AST_CONFIG_RULE`: a source-resolved call matched by `thread_api_config.json`.
- `STATIC_AST_POSITIONAL_BINDING`, `STATIC_AST_POSITIONAL_ASSIGNMENT`, `STATIC_AST_INITIALIZER_REFERENCE`: positional or syntactic flow inference.
- `STATIC_AST_HEURISTIC`, `STATIC_AST_TYPE_AND_SCOPE`: a heuristic/lexical-scope inference.
- `CLANG_PREPROCESSING_RECORD`: macro data from the detailed preprocessing record.
- `STATIC_AST_UNRESOLVED` / `UNRESOLVED`: a site that is visible but whose target was not resolved.

A high evidence grade describes the extraction method, not the severity of a bug. An extracted `DELETE` means a deletion operation occurs at that source site. It does not alone prove a use-after-free; aliases and execution ordering still require tracing.

## Required inputs

- A repository root containing the C/C++ source and header files.
- A build directory with `compile_commands.json` for that same repository and target configuration.
- Python with the `clang.cindex` binding compatible with the LibClang shared library.
- The corresponding LibClang binary.

The compile database is central. If it omits a source file, the extractor cannot discover that translation unit through this runner. If include directories/defines are stale, AST details may be incomplete. The extractor accepts generated source files when they belong to the repository and appear in the compilation database; it excludes build-output paths, `out`, `cmakefiles`, and `.git` paths.

## Linux runbook for the existing office setup

Use your existing office Linux environment where the Clang 21 bindings and LibClang 21 were already tested. Adjust the project paths to the repository checkout for tomorrow's run.

```bash
source ~/Desktop/neo4j/venv/bin/activate
python --version
python -c 'from clang import cindex; print("clang Python binding imported")'

# Confirm the compilation database exists and lists the intended sources.
python - <<'PY'
import json
from pathlib import Path
p = Path("/PATH/TO/REPOSITORY/build_linux/compile_commands.json")
entries = json.loads(p.read_text(encoding="utf-8-sig"))
print("compile entries:", len(entries))
print("distinct source files:", len({e.get("file", "") for e in entries}))
PY

python /PATH/TO/graph-extractor/extract_repository_graph_rca_v2.py \
  --repository-root /PATH/TO/REPOSITORY \
  --build-directory /PATH/TO/REPOSITORY/build_linux \
  --output-directory /PATH/TO/graph-output/rca_v2_scratch \
  --libclang /usr/lib/llvm-21/lib/libclang-21.so.1 \
  --thread-api-config /PATH/TO/graph-extractor/thread_api_config.json
```

For a small smoke run, add `--limit 1`. **A limited run must not be loaded as a complete graph**; the manifest marks it as partial. Continue a full run with `--resume`; failed TUs are skipped unless `--retry-failed` is also supplied. Start with a new scratch output directory rather than overwriting any existing extraction artifacts.

Before starting the run, inspect the `compile_commands.json` command arguments for stale paths/defines and confirm the `--repository-root` matches the checkout whose files appear in the database. Do not manually pass an output directory inside the source tree. The runner now validates the API config, compile database, source discovery, LibClang path and Python binding before it creates worker checkpoints. Use the no-write dry run first:

```bash
python /PATH/TO/graph-extractor/extract_repository_graph_rca_v2.py \
  --repository-root /PATH/TO/REPOSITORY \
  --build-directory /PATH/TO/REPOSITORY/build_linux \
  --output-directory /PATH/TO/graph-output/rca_v2_scratch \
  --libclang /usr/lib/llvm-21/lib/libclang-21.so.1 \
  --thread-api-config /PATH/TO/graph-extractor/thread_api_config.json \
  --dry-run
```

`--dry-run` lists selected project translation units and exits without invoking Clang or creating extraction output. A normal run fails early if `clang.cindex` cannot be imported by the selected Python interpreter.

## Post-extraction ground-truth validation

The extractor does not load ground truth. After an extraction of the ThreadDemo benchmark, run:

```bash
python /PATH/TO/graph-extractor/tools/check_graph_vs_truth.py \
  --output-directory /PATH/TO/graph-output/rca_v2_scratch \
  --ground-truth /PATH/TO/ThreadDemo/ground_truth.json
```

The checker compares each declared edge and writes `ground_truth_check.csv`. `FOUND` for `NO_LOCK` means the expected absence check passed: the function is located, nearby access evidence is present, and no matching lock event was extracted in that function's range. It does not create a graph edge labelled `NO_LOCK`.

Treat missing edges as an investigation queue. A miss may indicate: the compile DB did not include the file; Clang failed to parse it; macro locations were remapped; an API needs a config rule; or a target could not be statically resolved. Do not patch an extractor rule solely to make ground truth pass without checking the underlying AST/source first.

Before a downstream loader consumes the extracted files, run the generic artifact contract/integrity checker:

```bash
python /PATH/TO/graph-extractor/tools/validate_extraction_artifacts.py \
  --output-directory /PATH/TO/graph-output/rca_v2_scratch \
  --require-complete
```

It checks required files/headers, JSONL validity, duplicate IDs, common cross-artifact references and run completeness. Dangling references are warnings by default because partial TUs and external/template resolution can create legitimate gaps; use `--strict-references` only after reviewing the warnings for that repository. It never reads ground truth or writes Neo4j.

## Windows notes

The runner uses `pathlib` and accepts Windows paths, but it still requires a usable `compile_commands.json` and an installed Python Clang binding matched to the supplied LibClang DLL. Generate/export the compilation database from the exact build configuration. For Visual Studio/MSVC projects, a CMake compile database may require a CMake/Ninja-based build configuration; do not assume a `.sln` or `.vcxproj` automatically supplies `compile_commands.json`.

## Current limits, stated explicitly

- This implementation is a **C/C++ Clang adapter**, not a multi-language parser. The artifact contract is intended to make future Java/C#/JavaScript adapters possible, but those adapters are not implemented here.
- Static `compile_commands.json` analysis only covers the selected build configuration, source files, preprocessor defines and include search paths.
- Virtual dispatch, templates, function-pointer targets, wrapper functions, callbacks stored in objects, custom ownership APIs and dynamic libraries can remain unresolved. The output records that uncertainty instead of inventing targets.
- Pointer alias analysis is local/syntactic and does not establish a complete points-to set across the whole program.
- `std::move` denotes a move expression; its actual ownership effect depends on the selected overload and type. The moved-from state is not assumed for arbitrary objects.
- `shared_ptr` reference counts do not make concurrent mutation of the same `shared_ptr` variable safe. The extractor records types and operations but does not prove thread safety.
- `weak_ptr::lock()`/`expired()` and smart-pointer observer methods are operations, not proof of use-after-free.
- Lock scope is lexical evidence. A detected lock does not prove every relevant access is protected by the same mutex, especially through aliases or wrappers. `try_lock` is conditional; `defer_lock` is not acquisition.
- `std::atomic` operations are represented; a compound logical invariant may still be broken even when each operation is atomic.
- Macro expansion/definition association depends on the LibClang version and source mapping. Same-line macro associations are hints only.
- No runtime sequence, happens-before relationship, or log chronology is inferred from AST line order. The log parser and RCA pipeline supply runtime evidence separately.

## Explicitly out of scope for this package

This change does not create `Span` nodes, write to Neo4j, change the Neo4j schema/importer, or modify the LLM traversal/controller. The package emits extraction artifacts and a separate graph-ground-truth check only. Integrate the output with a loader in a later, reviewed change.
