# ThreadDemo S7-S13 runbook

## Compatibility and preservation

- The project remains C++17.
- No S1-S6 scenario function body was changed. Exact function hashes and line ranges are recorded in `S1_S6_preservation_manifest.json`.
- `backups/main.cpp.bak1` is the pre-integration backup of `src/main.cpp`. Only the `main()` body was changed in that existing file; S7-S13 implementations live in new `src/scenario_s*.cpp` files.
- Running without `--scenarios` still executes only S1-S6.
- New scenario source contains no `@GT` marker or answer-key role annotations.
- New ground truth is stored at `docs/ground_truth_s7_s13.json`, outside the `src` source root.

## Build

Linux:

```bash
cmake -S ThreadDemo -B ThreadDemo/build -DCMAKE_BUILD_TYPE=Debug
cmake --build ThreadDemo/build -j2
```

Windows PowerShell with CMake and a configured C++ compiler:

```powershell
cmake -S .\ThreadDemo -B .\ThreadDemo\build -DCMAKE_BUILD_TYPE=Debug
cmake --build .\ThreadDemo\build --config Debug
```

CMake exports `compile_commands.json` in the build directory. Use the actual build directory as the extractor's build-directory argument.

## Run commands

From the `ThreadDemo` directory on Linux:

```bash
./build/thread_demo --log logs/demo_run.log
./build/thread_demo --scenarios S7,S8 --log logs/s7_s8.log
./build/thread_demo --scenarios S9,S10,S11 --log logs/s9_s11.log
./build/thread_demo --scenarios S12,S13 --log logs/s12_s13.log
```

On Windows, run the generated executable path for the selected CMake generator, for example:

```powershell
.\build\Debug\thread_demo.exe --scenarios S7,S8 --log .\logs\s7_s8.log
```

Scenario selection is opt-in and accepts comma-separated IDs S1 through S13. `--repeat N` repeats only the selected scenarios; `--rounds N` still controls S2.

## Ground-truth validation

From the `ThreadDemo` directory:

```bash
python tools/validate_ground_truth_s7_s13.py
python tools/check_logs_s7_s13.py logs/sample_run_s7_s13.log
```

The ground-truth validator verifies that the source root is exactly `src`, that ground truth is outside it, and that each exact source snippet matches its specified line range. The log validator separately verifies that each S7-S13 scenario start and normalized ERROR anchor are present. Both validate S7-S13 separately from the original marker-based S1-S6 ground truth.

## Logs

- `logs/sample_run_01.log` is the original S1-S6 sample and remains unchanged.
- `logs/sample_run_s7_s13.log` is a generated, single-process run of S7-S13 from the implementation test.
- Regenerate a current run from source rather than treating the committed sample log as a substitute for runtime verification.

## Scenario coverage at a glance

- **S7:** eight distinct worker threads; T6 follows a pass-through call into the resource reset site. Other threads are gated observers; a separately synchronized audit marker is a decoy.
- **S8:** `shared_ptr::use_count()` is treated as an exclusivity guarantee even though an observer can acquire an owner from `weak_ptr` afterward. Gates keep the example free of a data race on `Resource::value`.
- **S9:** a `unique_ptr` is moved to a worker. The moved-from source is checked explicitly; the worker uses `release()` only when it immediately re-wraps the returned pointer in a `unique_ptr`.
- **S10:** the last strong owner is reset before a weak observer finishes. The correct diagnosis is expiration, not use-after-free.
- **S11:** `RELEASE_AND_CLEAR` hides a `unique_ptr::reset()` behind a macro; inspect both the expansion call site and macro definition.
- **S12:** a deferred reference-capturing callback outlives its stack local, but an atomic lifetime token prevents a dangling-reference read. The error is the expired callback contract, not a demonstrated invalid dereference.
- **S13:** two paths use different mutexes around an atomic check-then-decrement. The final negative count is a logical invariant violation, not an atomic data race.

## Extractor integration status

The uploaded `graph-extractor.zip` did not include the spawn-aware P3 implementation, `EXTRACTOR_CHANGES.md`, `P3_match_rules.md`, graph loader/importer/schema, or the RCA `traversal/` package. Do not use that snapshot to replace an existing spawn-to-graph implementation. The graph-extraction gap report must be completed against the current P3 code and a scratch extraction once that package is available.
