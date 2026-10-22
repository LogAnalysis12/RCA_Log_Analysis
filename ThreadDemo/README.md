# ThreadDemo: concurrency bug test project for the log-analysis pipeline

A small C++17 program with **six scenarios**. It writes logs in the SAME format as the real DriverWrapper log
(`timestamp | perf_timer | thread_id | LEVEL | Class::Function message`), including real thread IDs.
It lets you test the pipeline (extract with Clang -> Neo4j -> traverse with LLM) on race / use-after-delete
bugs WITHOUT any confidential code.

| ID | What happens | Class | Correct answer |
|----|--------------|-------|----------------|
| S1 | `var` created in `CScenarioRunner::runS1`, shared with T1 (`q1 -> q2`) and T2 (`q3`). q3 deletes it. | DELETE_RACE | q3 deletes while q2 uses it |
| S2 | `CSettings::m_mode` changed by an `std::async` thread while a lambda thread reads it (5 repeats = 1 cluster) | VALUE_WRITE_RACE | `applyProfile` writes, `execute` reads, no lock |
| S3 | Writer locks a mutex, the auditor (native OS thread) does not | PARTIAL_LOCK_INVARIANT | lock exists on one side only |
| S4 | Both sides lock the same mutex; error is an ordering problem | PROTECTED_ORDERING (control) | NOT a race |
| S5 | `vector::clear()` on one pool worker, `at(0)` on another; spawn hidden behind `std::function` | CONTAINER_CLEAR_RACE_POOL | invalidate clears while consume reads |
| S6 | Classic parameter bug, single thread | PARAMETER_BUG (regression) | `selectService` returns "" for index 3 |

The ERROR line is always logged by the VICTIM function. The culprit is a different function on a different thread.
Several scenarios log the generic message `Exception occured: Unknown exception`, so the message alone never names the cause.

## Files
- `src/` the C++ code (file1_service, file2_settings, file3_owner, main, common/). Names mimic the real project (`CLoggerParser::Instance()->Writelog(...)`, `DEBUG_LOG`, `FUNCTION_START`).
- `// @GT ...` comments mark the true culprit/victim/spawn/lock statements. `tools/make_ground_truth.py` turns them into `ground_truth.json` (with file and line), so the answer key survives code edits.
- `tools/scenarios_meta.json` expected conclusion, wrong conclusions, thread roots per scenario.
- `tools/check_logs.py` validates a log with the real project's LOG_LINE and fingerprint regexes and checks the cross-thread runtime evidence.
- `tools/gen_compile_commands.py` writes `build/compile_commands.json` for the graph extractor (when you do not use CMake + Ninja).
- `logs/sample_run_01.log` a real run (`--repeat 2`) generated on Linux. Regenerate it on your machine for real Windows thread IDs.

## Build and run

Linux / WSL:
```
g++ -std=c++17 -Isrc src/*.cpp src/common/*.cpp -pthread -o build/thread_demo
./build/thread_demo --repeat 2 --log logs/run1.log
```
Windows with clang (PowerShell):
```
mkdir build -Force
clang++ -std=c++17 -Isrc (Get-ChildItem src -Recurse -Filter *.cpp).FullName -o build\thread_demo.exe
.\build\thread_demo.exe --repeat 2 --log logs\run1.log
```
Windows with MSVC (Developer PowerShell): `cl /EHsc /std:c++17 /Isrc src\*.cpp src\common\*.cpp /Fe:build\thread_demo.exe`

Options: `--repeat N` runs all scenarios N times, `--rounds N` repeats S2 N times, `--log PATH` sets the output file.

## Check the results
```
python tools/make_ground_truth.py
python tools/check_logs.py logs/run1.log
```
Expect `ALL SCENARIOS OK`. Then run your own `parse_logs.py` and `fingerprint.py` on the log: you should get 6 clusters.

## Notes
- Timing uses sleeps (>= 15 ms margins) so the interleaving is repeatable. On a very busy machine, if `check_logs.py` reports FAIL, re-run.
- The program never reads freed memory: the "use after delete" is detected by a nullptr check, so it is safe to run. The accesses are still unsynchronised, so ThreadSanitizer (`-fsanitize=thread` on Linux) reports them. That gives a ready-made input for a later sanitizer-report parser.
- The native thread in S3 uses `CreateThread` on Windows and `pthread_create` elsewhere; the thread function is passed to the wrapper `StartNativeThread`.
