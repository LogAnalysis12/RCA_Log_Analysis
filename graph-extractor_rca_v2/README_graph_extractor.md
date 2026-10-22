# Resumable C/C++ Repository Graph Extractor

## 1. Purpose

This folder contains the validated, resumable extraction workflow used to generate repository-wide neutral graph artifacts from a C/C++ codebase by using Clang/LibClang and an existing `compile_commands.json` compilation database.

The final recommended execution chain is:

```text
extract_repository_graph_resumable.py
        |
        +--> extract_tu_worker.py
                  |
                  +--> extract_graph_step3_fields_working.py
        |
        +--> checkpoint JSON files
        +--> worker log files
        +--> merged repository extraction artifacts
```

The resumable orchestrator is the main file that should be run. The worker and validated extractor module are invoked internally.

> Important: This workflow produces the merged repository extraction artifacts. If a separate graph-normalizer folder is used in the project, run that normalization stage afterward to produce the final normalized neutral graph used for database conversion.

---

## 2. Recommended Files

### `extract_repository_graph_resumable.py`

This is the final repository-level entry point.

Responsibilities:

- Reads `compile_commands.json` from the build directory.
- Discovers project-owned C/C++ translation units.
- Excludes files outside the repository and known build/generated paths.
- Starts one isolated worker process per translation unit.
- Saves one checkpoint JSON file per translation unit.
- Supports resuming completed extraction work.
- Supports retrying previously failed translation units.
- Rebuilds merged artifacts after every processed unit.
- Deduplicates Functions, calls, DataFields, and field-access records.
- Validates missing caller, callee, Function, and DataField identifiers.
- Generates the repository-level extraction report and manifest.

Run this file for the final extraction.

### `extract_tu_worker.py`

This is the isolated translation-unit worker.

Responsibilities:

- Receives one source file from the resumable orchestrator.
- Loads LibClang.
- Loads the validated extractor module dynamically.
- Retrieves the source file's compile command from the compilation database.
- Sanitizes compiler arguments through the validated extractor module.
- Parses one translation unit.
- Extracts Functions, calls, DataFields, and field accesses.
- Classifies diagnostics into project and external diagnostics.
- Writes one atomic checkpoint file.

Do not normally run this file manually. The resumable orchestrator calls it automatically.

### `extract_graph_step3_fields_working.py`

This is the validated single-translation-unit extraction module loaded by the worker.

The worker expects this module to provide the following functions:

```text
sanitize_compile_arguments(...)
classify_diagnostics(...)
extract_functions(...)
extract_calls(...)
extract_field_accesses(...)
```

The Step 3 working module contains the complete validated extraction chain for:

- Function definitions
- Project calls
- External or unresolved calls
- DataField declarations
- `READ`
- `WRITE`
- `READ_WRITE`
- `POSSIBLE_WRITE`

The earlier Step 1 and Step 2 files are historical development checkpoints and are not separately invoked by the final worker.

---

## 3. Historical or Reference Files

### `extract_repository_graph_runner_working.py`

This is the validated non-resumable repository runner.

It parses all translation units in the main Python process and generates the same main extraction artifacts. It remains useful as a reference or fallback, but it does not provide the same worker isolation and checkpoint/resume protection as the resumable workflow.

### `extract_repository_graph.py`

This is another non-resumable repository-runner copy or renamed development version. Do not use it as the primary final entry point.

### `extract_repository_graph_before_resume_support.py`

This is the preserved repository runner from before resume support and worker isolation were added. Keep it only as a historical backup.

### Older extraction stages

The following files are development-stage or backup implementations:

```text
extract_functions.py
extract_functions_step1_working.py
extract_graph.py
extract_graph_fields_backup.py
extract_graph_step2_calls_working.py
extract_graph_step3_fields.py
```

Do not run these files for the final repository extraction.

---

## 4. Required Inputs

The main file, `extract_repository_graph_resumable.py`, requires four mandatory inputs.

### 4.1 Repository root

Argument:

```text
--repository-root
```

Description:

The root folder of the C/C++ repository being extracted.

Requirements:

- The folder must exist.
- Project source and header files must be available beneath the root.
- The repository folder name becomes `repository_id` in the extraction output.
- Exported source paths are stored relative to this root.

Example:

```text
C:\Work\DriverWrapperRepo
```

### 4.2 Build directory

Argument:

```text
--build-directory
```

Description:

The directory containing `compile_commands.json`.

Requirements:

- The directory must exist.
- The file below must exist:

```text
<build-directory>\compile_commands.json
```

- Each translation unit to be extracted must have a valid compilation command.
- Include paths, macros, language-standard options, and generated-header paths must be represented in the compilation commands.

Example:

```text
C:\Work\DriverWrapperRepo\out\build\x86-debug
```

### 4.3 Output directory

Argument:

```text
--output-directory
```

Description:

The destination for checkpoints, worker logs, merged extraction files, reports, and the manifest.

The script creates the directory structure when required.

Use a new module-specific output folder for each extraction run.

Example:

```text
C:\Work\graph-export\driverwrapper-raw
```

### 4.4 LibClang path

Argument:

```text
--libclang
```

Description:

The complete path to `libclang.dll`.

Example:

```text
C:\Program Files\LLVM\bin\libclang.dll
```

The Python Clang bindings must be compatible with the installed LibClang version.

---

## 5. Optional Inputs

### `--extractor-module`

Path to the validated extraction module.

Default:

```text
.\graph-extractor\extract_graph_step3_fields_working.py
```

Specify an explicit absolute path when running from a different working directory.

### `--worker-script`

Path to the isolated translation-unit worker.

Default:

```text
.\graph-extractor\extract_tu_worker.py
```

Specify an explicit absolute path when running from a different working directory.

### `--limit`

Processes only the first N discovered translation units.

Default:

```text
0
```

A value of `0` means process all translation units.

Use a small value during validation:

```text
--limit 1
```

### `--resume`

Reuses existing checkpoint files.

Behavior:

- Successful checkpoints are skipped.
- Failed checkpoints are skipped unless `--retry-failed` is also used.

### `--retry-failed`

When used with `--resume`, previously failed translation units are attempted again.

---

## 6. Prerequisites

### Python environment

Activate the existing environment:

```powershell
cd "C:\path\to\project"
.\.venv\Scripts\Activate.ps1
```

Confirm Python:

```powershell
python --version
```

### Python packages

The environment must contain the Python Clang bindings.

Validation command:

```powershell
python -c "from clang import cindex; print('Python Clang binding is available')"
```

### LLVM and LibClang

Confirm Clang:

```powershell
clang --version
```

Confirm `libclang.dll` exists:

```powershell
Test-Path "C:\Program Files\LLVM\bin\libclang.dll"
```

Expected:

```text
True
```

### Compilation database

Confirm the build directory contains `compile_commands.json`:

```powershell
Test-Path "C:\path\to\build-directory\compile_commands.json"
```

Expected:

```text
True
```

If the file is missing, generate the compilation database using the module's approved build workflow before running extraction.

---

## 7. Validate the Scripts Before Running

Compile the three final Python files:

```powershell
python -m py_compile `
    ".\graph-extractor\extract_repository_graph_resumable.py" `
    ".\graph-extractor\extract_tu_worker.py" `
    ".\graph-extractor\extract_graph_step3_fields_working.py"
```

No output means compilation passed.

Display the main command-line interface:

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" --help
```

---

## 8. Recommended Execution Flow

### Step 1: Create an output folder

```powershell
$OutputDirectory = "C:\Work\graph-export\driverwrapper-raw"
New-Item -ItemType Directory $OutputDirectory -Force | Out-Null
```

### Step 2: Test one translation unit

Use `--limit 1` before processing the full repository:

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\DriverWrapperRepo" `
    --build-directory "C:\Work\DriverWrapperRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\driverwrapper-test" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py" `
    --limit 1
```

Review the test output and `extraction_report.json` before running the complete repository.

### Step 3: Run the full repository

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\DriverWrapperRepo" `
    --build-directory "C:\Work\DriverWrapperRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\driverwrapper-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py"
```

### Step 4: Resume an interrupted run

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\DriverWrapperRepo" `
    --build-directory "C:\Work\DriverWrapperRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\driverwrapper-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py" `
    --resume
```

### Step 5: Retry failed translation units

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\DriverWrapperRepo" `
    --build-directory "C:\Work\DriverWrapperRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\driverwrapper-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py" `
    --resume `
    --retry-failed
```

---

## 9. Translation Unit Discovery Rules

The resumable orchestrator reads `compile_commands.json` and selects source files with these extensions:

```text
.c
.cc
.cpp
.cxx
```

A source file is selected only when:

- The source file exists.
- The source file lies inside the repository root.
- The extension is supported.
- The normalized path does not contain an excluded fragment.

Excluded path fragments currently include:

```text
/out/
/build/
/generated/
/cmakefiles/
/.git/
```

Review these exclusions for DServer and OTX. If project-owned generated source must be included, update the exclusion policy deliberately and validate the resulting graph.

---

## 10. Checkpoint and Resume Behavior

The output directory contains:

```text
checkpoints\
worker-logs\
```

Each translation unit receives a deterministic checkpoint filename based on:

- Repository-relative source path
- A SHA-256 digest of the relative path

A successful checkpoint contains:

```text
relative_path
source_file
status
worker_exit_code
duration_seconds
functions
calls
data_fields
data_accesses
diagnostics
diagnostic_summary
error
traceback
```

The worker writes the checkpoint through a temporary file and then atomically replaces the target checkpoint.

The parent process rebuilds merged repository artifacts after every translation unit. Partial successful progress therefore remains usable even when a later worker crashes.

---

## 11. Generated Extraction Artifacts

The repository-level output directory contains:

### `functions.jsonl`

One JSON Function record per line.

Important fields include:

```text
function_id
repository_id
class_name
function_name
qualified_name
signature
relative_path
start_line
end_line
content_hash
source_code
cursor_kind
```

### `calls.csv`

Project, external, and unresolved call occurrences.

Columns:

```text
caller_function_id
callee_function_id
callee_name
qualified_callee_name
callee_signature
call_line
call_column
call_type
resolution_status
call_expression
```

### `data_fields.csv`

Persistent project DataFields, including class fields and allowed global or namespace variables.

Columns:

```text
field_id
repository_id
field_name
qualified_name
field_type
declaration_relative_path
declaration_line
declaration_kind
```

### `data_access.csv`

Function-to-DataField access occurrences.

Columns:

```text
function_id
field_id
chain
suffix2
field_name
field_type
access_type
line
column
```

Supported access types:

```text
READ
WRITE
READ_WRITE
POSSIBLE_WRITE
```

### `translation_units.csv`

Per-translation-unit execution and diagnostics summary.

Important columns include:

```text
relative_path
status
duration_seconds
functions
calls
data_fields
data_accesses
project_errors
external_errors
worker_exit_code
error
```

### `extraction_report.json`

Repository-level counts and integrity checks.

Important properties include:

```text
translation_units_recorded
translation_units_successful
translation_units_failed
unique_functions
unique_calls
unique_data_fields
unique_data_accesses
missing_caller_ids
missing_resolved_callee_ids
missing_access_function_ids
missing_access_field_ids
call_resolution_status_counts
access_type_counts
duration_seconds
```

### `manifest.json`

Describes the extraction schema and generated files.

The resumable version reports:

```text
schema_version: 1.1
resume_supported: true
worker_isolation: true
paths_are_repository_relative: true
```

---

## 12. Basic Validation Commands

Load the extraction report:

```powershell
$Report = Get-Content "C:\Work\graph-export\driverwrapper-raw\extraction_report.json" -Raw | ConvertFrom-Json
$Report | Format-List
```

Required integrity expectations:

```text
missing_caller_ids: 0
missing_access_function_ids: 0
missing_access_field_ids: 0
```

`missing_resolved_callee_ids` should be reviewed. A non-zero value may indicate a cross-translation-unit or cross-module target that requires later normalization.

Inspect failed translation units:

```powershell
Import-Csv "C:\Work\graph-export\driverwrapper-raw\translation_units.csv" |
Where-Object status -ne "SUCCESS" |
Format-Table -Wrap -AutoSize
```

Inspect project parsing errors:

```powershell
Import-Csv "C:\Work\graph-export\driverwrapper-raw\translation_units.csv" |
Where-Object { [int]$_.project_errors -gt 0 } |
Select-Object relative_path, project_errors, error |
Format-Table -Wrap -AutoSize
```

Show output counts:

```powershell
$Output = "C:\Work\graph-export\driverwrapper-raw"

[PSCustomObject]@{
    Functions   = (Get-Content "$Output\functions.jsonl" | Measure-Object -Line).Lines
    Calls       = (Import-Csv "$Output\calls.csv").Count
    DataFields  = (Import-Csv "$Output\data_fields.csv").Count
    DataAccess  = (Import-Csv "$Output\data_access.csv").Count
    Units       = (Import-Csv "$Output\translation_units.csv").Count
} | Format-List
```

---

## 13. Extraction Versus Final Normalization

The resumable extractor performs repository-level merging and deduplication, but the project may also contain a separate normalization and export stage.

Recommended complete flow:

```text
C/C++ repository
    |
    v
extract_repository_graph_resumable.py
    |
    v
Raw/merged extraction artifacts
    |
    v
Graph normalizer
    |
    v
Validated normalized neutral graph
    |
    +--> Neptune converter/export
    |
    +--> Neo4j converter/import
```

Do not send Neptune-specific export files as the database-neutral source of truth.

For the validated DriverWrapper work, the final normalized graph was kept separately from the raw extractor folder. Preserve this separation for DServer and OTX.

---

## 14. DServer and OTX Reuse

For another module, change only the module-specific paths:

```text
--repository-root
--build-directory
--output-directory
```

The LibClang, worker, and validated extractor paths can remain the same when the environment is shared.

Example DServer command:

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\DServerRepo" `
    --build-directory "C:\Work\DServerRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\dserver-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py"
```

Example OTX command:

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\Work\OTXRepo" `
    --build-directory "C:\Work\OTXRepo\out\build\x86-debug" `
    --output-directory "C:\Work\graph-export\otx-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py"
```

The DServer and OTX repositories must each have a valid compilation database generated from their actual build context.

---

## 15. Known Scope of the Current Extractor

The validated Step 3 extractor currently models:

```text
Function definitions
Project and external calls
Persistent DataFields
Field reads and writes
```

Local variables and function parameters are intentionally excluded from DataField extraction. Parameter, call-site argument, and return-value flow require a future schema and extractor enhancement before creating the final integrated DriverWrapper, DServer, and OTX graph.

---

## 16. Troubleshooting

### No translation units are selected

Check:

- `compile_commands.json` exists.
- File paths in the compilation database exist.
- Source files are inside the repository root.
- Source paths are not excluded by `/out/`, `/build/`, `/generated/`, `/cmakefiles/`, or `/.git/`.

### No compilation command found

The worker requires an exact compile-command entry for each selected source file. Regenerate or correct `compile_commands.json`.

### LibClang cannot load

Verify:

```powershell
Test-Path "C:\Program Files\LLVM\bin\libclang.dll"
```

Confirm the Python Clang binding is compatible with the installed library.

### Worker crashes

Review the corresponding file in:

```text
<output-directory>\worker-logs\
```

The orchestrator records a `CRASHED` checkpoint so the remaining translation units can continue.

Retry after correcting the cause:

```powershell
--resume --retry-failed
```

### Successful translation units are processed again

Use the same output directory and include:

```text
--resume
```

### Output contains unresolved project calls

Review `resolution_status` and preserve the records. Later module or cross-module normalization may resolve these targets.

---

## 17. Final Operational Checklist

Before the full run:

- [ ] Repository root exists.
- [ ] Build directory exists.
- [ ] `compile_commands.json` exists.
- [ ] `libclang.dll` exists.
- [ ] Python Clang binding imports successfully.
- [ ] Final three Python files compile.
- [ ] `--limit 1` test succeeds.
- [ ] Output folder is module-specific.

After the full run:

- [ ] All expected translation units are recorded.
- [ ] Failed translation units are reviewed.
- [ ] `missing_caller_ids` equals zero.
- [ ] Missing DataField endpoints equal zero.
- [ ] Project parser errors are reviewed.
- [ ] Output counts are recorded.
- [ ] The graph normalizer is run if required.
- [ ] The final normalized neutral graph is validated.
- [ ] The final package and SHA-256 hash are created.

---

## 18. Quick Start

```powershell
python ".\graph-extractor\extract_repository_graph_resumable.py" `
    --repository-root "C:\path\to\repository" `
    --build-directory "C:\path\to\build-directory" `
    --output-directory "C:\path\to\graph-export\module-raw" `
    --libclang "C:\Program Files\LLVM\bin\libclang.dll" `
    --extractor-module ".\graph-extractor\extract_graph_step3_fields_working.py" `
    --worker-script ".\graph-extractor\extract_tu_worker.py" `
    --limit 1
```

If the test passes, remove `--limit 1` for the complete extraction.
