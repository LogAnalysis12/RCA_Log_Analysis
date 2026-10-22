# DriverWrapper / ConXi Log Analysis POC

## Overview

This project parses real DriverWrapper logs, creates deterministic error clusters, resolves each representative error to the C/C++ hybrid graph in local Neo4j, retrieves hash-verified local source, and uses a tool-driven Investigator plus an independent Critic to produce an evidence-backed root-cause finding.

The first real scenario was validated by a developer who works on the code. The optimized accepted run used 8 Bedrock calls, 58,800 total tokens, approximately USD 0.23368, and 71.349 seconds.

## Architecture

```text
Raw log -> Task 29 parser -> ERROR records
        -> Task 30 normalization/fingerprinting/clustering
        -> Task 31 Neo4j Function anchor
        -> verified C++ source + graph tools
        -> Bedrock Investigator
        -> deterministic verifier
        -> independent Critic
        -> investigation_result.json
```

Tasks 29-31 are deterministic and do not use an LLM. Bedrock is used only for the Investigator and Critic stage.

## Project structure

```text
log-analysis-project/
|-- main.py                     # Runs Tasks 29, 30, and 31
|-- parse_logs.py               # Task 29: parse the real pipe-separated log and keep exact ERROR rows
|-- fingerprint.py              # Task 30: normalize messages, create fingerprints, cluster occurrences
|-- neptune_anchor.py           # Task 31: resolve representative errors to Neo4j Function nodes
|-- config.example.json         # Safe configuration template
|-- config.json                 # Local configuration; never store AWS keys here
|-- requirements.txt            # Python dependencies
|-- run.ps1                     # Optional PowerShell shortcut for main.py
|-- input/                      # Real log input
|-- output/                     # Generated parser, cluster, anchor, traversal, and golden-case results
|-- traversal/
|   |-- __init__.py
|   |-- source_reader.py        # Safe path resolution, exact source range reading, hash verification
|   |-- neptune_tools.py        # read source, callers, callees, fields, and field writers
|   |-- llm_backends.py         # Bedrock-first backend plus optional local OpenAI-compatible backend
|   |-- backend_factory.py      # Creates Investigator/Critic backends and applies shared budgets
|   |-- schemas.py              # Investigation state and structured result types
|   |-- prompts.py              # Investigator and Critic system instructions
|   |-- tool_definitions.py     # Canonical tool schemas
|   |-- compact_results.py      # Canonical IDs, compact model payloads, evidence signatures
|   |-- budget.py               # Call/token/cost limits and metrics
|   |-- verifier.py             # Deterministic source, citation, route, and boundary checks
|   |-- critic.py               # Independent Critic invocation and JSON result parsing
|   |-- investigator.py         # Tool-using loop, retries, duplicate guards, verifier/Critic orchestration
|   |-- run_traversal.py        # Controlled paid live traversal and result/metric persistence
|   |-- test_tools.py           # Free deterministic Neo4j/source validation
|   |-- test_agent_mock.py      # Free no-AWS orchestration test
|   `-- test_bedrock.py         # Minimal Bedrock connectivity test
```

## Prerequisites

1. Windows PowerShell and Python 3.10 or later.
2. A local C/C++ source snapshot matching the graph content hashes.
3. AWS credentials configured through the approved default credential chain.
4. Local Neo4j container running for Tasks 31 and traversal.
5. Neo4j Bolt port 7687 published to localhost by Docker.
6. Bedrock permission `bedrock:InvokeModel` for the selected inference profile.
7. Do not put AWS access keys, secret keys, or session tokens in the project.

## First-time local setup

```powershell
cd "C:\path\to\log-analysis-project"
python -m venv ".venv"
.\.venv\Scripts\Activate.ps1
python -m pip install -r ".\requirements.txt"
Copy-Item ".\config.example.json" ".\config.json"
New-Item -ItemType Directory ".\input" -Force | Out-Null
```

Place the real log under `input/` and update `config.json`.

## Local Neo4j

Start a local database (PowerShell):

```powershell
$env:NEO4J_PASSWORD = "change-this-local-password"
docker run --name neo4j -p 7474:7474 -p 7687:7687 -e "NEO4J_AUTH=neo4j/$env:NEO4J_PASSWORD" -d neo4j:5
```

The importer in `tools/load_neo4j.py` accepts the database-neutral export directory. Run `python tools/load_neo4j.py <neutral-graph-dir> --reset` with `NEO4J_PASSWORD` set. Its ID conventions and the access-type relationship mapping are documented in the migration report.

Install test dependencies with `python -m pip install -r requirements-dev.txt`; run offline checks with `python -m pytest -m "not neo4j and not neo4j_real"`. Disposable live checks are available through `tools/run_live_tests.ps1` and `tools/run_live_tests.sh`.

## Configuration

```json
{
  "input_log": "./input/CON_X_DWrapper_2026-09-07-120717_0.txt",
  "output_directory": "./output",
  "source_root": "C:\\path\\to\\DriverWrapperRepo_LLM",
  "graph": {
    "provider": "neo4j",
    "uri": "bolt://localhost:7687",
    "username": "neo4j",
    "database": "neo4j",
    "password_env": "NEO4J_PASSWORD"
  },
  "llm": {
    "provider": "bedrock",
    "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "region": "us-east-1",
    "max_tokens": 1024,
    "temperature": 0.0
  },
  "traversal": {
    "ignored_fields": ["DEBUG_LOG", "INFO_LOG", "WARN_LOG", "ERROR_LOG"],
    "ignored_project_callees": [
      "CLoggerParser::Instance",
      "CLoggerParser::Writelog",
      "CDWrapperExecption::CDWrapperExecption"
    ],
    "ignored_external_prefixes": [
      "std::basic_string",
      "std::operator+",
      "std::operator<<",
      "std::basic_ostream",
      "std::exception::what"
    ],
    "max_callers": 30,
    "max_callees": 40,
    "max_field_writers": 30,
    "max_steps": 8,
    "max_critic_rounds": 3,
    "stall_retries": 1,
    "repeated_tool_limit": 1,
    "max_bedrock_calls": 10,
    "max_total_input_tokens": 100000,
    "max_total_output_tokens": 15000,
    "max_total_tokens": 115000,
    "max_estimated_cost_usd": 0.35,
    "compact_tool_results": true,
    "max_source_characters_per_function": 12000,
    "max_tool_result_characters": 16000,
    "prevent_equivalent_conclusions": true,
    "pricing": {
      "input_per_million_tokens_usd": 3.30,
      "output_per_million_tokens_usd": 16.50
    }
  }
}
```

Set `NEO4J_PASSWORD` in the process environment. The URI should use `bolt://localhost:7687` for local Docker. Never put the password in config.json.

Validate configuration:

```powershell
Get-Content ".\config.json" -Raw | ConvertFrom-Json | Out-Null
```

## Run the project

### Full deterministic preprocessing and anchor resolution

Start Neo4j Docker container and wait for Bolt to accept connections.

```powershell
python ".\main.py" --config ".\config.json"
```

This creates:

```text
output/
|-- task29_parsing/
|   |-- parsed_records.jsonl
|   |-- errors.jsonl
|   |-- run_metadata.json
|   `-- parse_report.json
|-- task30_fingerprints/
|   |-- error_clusters.json
|   `-- fingerprint_report.json
|-- task31_anchors/
|   |-- resolved_issues.json
|   `-- anchor_report.json
`-- pipeline_report.json
```

Run Tasks 29 and 30 without connecting to Neo4j:

```powershell
python ".\main.py" --config ".\config.json" --skip-graph
```

### Free deterministic graph/source validation

```powershell
python -m traversal.test_tools --config ".\config.json"
Write-Host "Exit code:" $LASTEXITCODE
```

### Free Investigator/Critic orchestration validation

```powershell
python -m traversal.test_agent_mock
Write-Host "Exit code:" $LASTEXITCODE
```

Every mock validation value should be `True`.

### Minimal Bedrock connectivity test

```powershell
python -m traversal.test_bedrock --config ".\config.json"
```

### Controlled paid traversal

```powershell
python -m traversal.run_traversal --config ".\config.json"
```

The runtime stops on `ACCEPTED`, `NOT_ACCEPTED`, `STALLED`, `MAX_STEPS_REACHED`, `BUDGET_EXCEEDED`, or runtime failure. The console and JSON result include calls, tokens, estimated cost, and elapsed time.

## View results

```powershell
$Result = Get-Content ".\output\investigation_result.json" -Raw | ConvertFrom-Json
```

Overall status:

```powershell
$Result.status
```

Execution metrics:

```powershell
$Result.execution_metrics | Format-List
```

Conclusion:

```powershell
$Result.conclusion | Format-List
```

Deterministic verification:

```powershell
$Result.verification | Format-List
```

Critic verdict:

```powershell
$Result.critic | Select-Object verdict, confidence, reasons, continuation_instruction | Format-List
```

Traversal sequence:

```powershell
$Result.state.tool_history |
Select-Object step, tool, input |
Format-Table -Wrap -AutoSize
```

Verified Functions read:

```powershell
$Result.state.read_functions.PSObject.Properties |
ForEach-Object {
  [PSCustomObject]@{
    FunctionId = $_.Name
    QualifiedName = $_.Value.function.qualified_name
    File = $_.Value.function.relative_path
    StartLine = $_.Value.source.start_line
    EndLine = $_.Value.source.actual_end_line
    SourceStatus = $_.Value.status
  }
} | Format-Table -Wrap -AutoSize
```

Correction feedback:

```powershell
$Result.state.critic_feedback
```

## Validated golden case

```text
Error function: CDServerCommunication::CreateDiagComPrimitiveByName
Status: ACCEPTED
Developer validation: Correct
Elapsed time: 71.349 seconds
Bedrock calls: 8
Total tokens: 58,800
Estimated cost: USD 0.23368
```

Preserve the result:

```powershell
Copy-Item ".\output\investigation_result.json" ".\output\golden_case_01_validated.json" -Force
Copy-Item ".\traversal" ".\traversal_golden_case_01" -Recurse -Force
```

## Cost and operational controls

- Stop the local Neo4j container when it is not needed.
- Keep the SageMaker notebook stopped unless notebook execution is required.
- Bedrock on-demand does not need to be stopped. Cost occurs only when requests are invoked.
- The cost ceiling is enforced before the next call. A completed response can push the final estimate slightly over the threshold because usage is returned after the response.
- Full tool results remain in local audit output; compact results are sent to Bedrock.

## Troubleshooting

### `Input log not found`

Confirm `input_log` and place the file under `input/`.

```powershell
Test-Path ".\input\CON_X_DWrapper_2026-09-07-120717_0.txt"
```

### Neo4j connection timeout

Check that the local container is running, that port 7687 is published, and that the configured password matches `NEO4J_PASSWORD`.

```powershell
Test-NetConnection -ComputerName "localhost" -Port 7687
```

### `AccessDeniedException`

Confirm the Neo4j container is running, Bolt is published on port 7687, and `NEO4J_PASSWORD` matches `NEO4J_AUTH`. Bedrock still uses the configured AWS credentials only when a Bedrock traversal is explicitly run.

### `BUDGET_EXCEEDED`

This is a controlled stop. Inspect:

```powershell
$Result.status
$Result.budget_reason
$Result.execution_metrics | Format-List
```

### Repeated or unsupported conclusion

Inspect:

```powershell
$Result.state.critic_feedback
$Result.state.tool_history | Select-Object step, tool, input | Format-Table -Wrap -AutoSize
```

## Development rules

1. Do not let the LLM execute arbitrary openCypher. Use approved deterministic tools.
2. Do not accept a conclusion unless the blamed source was read and hash-verified.
3. Do not treat an exception message as the suspicious application value.
4. Do not claim internal behavior for unavailable external source.
5. Preserve full audit evidence locally while sending bounded context to Bedrock.
6. Run `test_agent_mock` and `test_tools` before every paid traversal after runtime changes.


## Single command to get all the results from json files 

1: Copy the current file invegtigation_result.json into the another .json file with with proper name. 

2: Run this command by changing the name at the last line:

& {
    param(
        [string]$File
    )

    if (-not (Test-Path $File)) {
        Write-Host "Result file not found: $File" -ForegroundColor Red
        return
    }

    $Result = Get-Content $File -Raw | ConvertFrom-Json

    Write-Host "`n============================================================" -ForegroundColor Cyan
    Write-Host "              INVESTIGATION RESULT REVIEW" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "File: $File"
    Write-Host "Last modified: $((Get-Item $File).LastWriteTime)"
    Write-Host "SHA256: $((Get-FileHash $File -Algorithm SHA256).Hash)"

    Write-Host "`n========== OVERALL STATUS ==========" -ForegroundColor Yellow
    Write-Host "Status:" $Result.status
    Write-Host "Reviewed conclusions:" $Result.reviewed_conclusion_count
    Write-Host "Critic rejections:" $Result.critic_rejection_count
    Write-Host "Budget reason:" $Result.budget_reason

    Write-Host "`n========== SELECTED ISSUE ==========" -ForegroundColor Yellow
    if ($null -ne $Result.selected_issue) {
        $Result.selected_issue | Format-List
    }
    elseif ($null -ne $Result.state.issue) {
        $Result.state.issue |
        Select-Object `
            fingerprint_id,
            occurrence_count,
            qualified_function,
            normalized_message |
        Format-List
    }
    else {
        Write-Host "Selected issue metadata was not found."
    }

    Write-Host "`n========== EXECUTION METRICS ==========" -ForegroundColor Yellow
    if ($null -ne $Result.execution_metrics) {
        $Result.execution_metrics | Format-List
    }
    else {
        Write-Host "Execution metrics were not recorded."
    }

    Write-Host "`n========== FINAL CONCLUSION ==========" -ForegroundColor Yellow
    if ($null -ne $Result.conclusion) {
        $Result.conclusion | Format-List
    }
    else {
        Write-Host "No final conclusion was produced."
    }

    Write-Host "`n========== DETERMINISTIC VERIFICATION ==========" -ForegroundColor Yellow
    if ($null -ne $Result.verification) {
        $Result.verification | Format-List
    }
    else {
        Write-Host "No conclusion reached deterministic verification."
    }

    Write-Host "`n========== INDEPENDENT CRITIC ==========" -ForegroundColor Yellow
    if ($null -ne $Result.critic) {
        $Result.critic |
        Select-Object `
            verdict,
            confidence,
            reasons,
            continuation_instruction,
            usage |
        Format-List
    }
    else {
        Write-Host "The independent Critic was not called."
    }

    Write-Host "`n========== FEEDBACK AND REJECTIONS ==========" -ForegroundColor Yellow
    $Feedback = @($Result.state.critic_feedback)

    if ($Feedback.Count -gt 0) {
        for ($Index = 0; $Index -lt $Feedback.Count; $Index++) {
            Write-Host "$($Index + 1). $($Feedback[$Index])"
        }
    }
    else {
        Write-Host "No verifier or Critic feedback was recorded."
    }

    Write-Host "`n========== TOOL USAGE SUMMARY ==========" -ForegroundColor Yellow
    $ToolHistory = @($Result.state.tool_history)

    if ($ToolHistory.Count -gt 0) {
        $ToolHistory |
        Group-Object tool |
        Sort-Object Count -Descending |
        Select-Object `
            @{Name = "Calls"; Expression = {$_.Count}},
            @{Name = "Tool"; Expression = {$_.Name}} |
        Format-Table -AutoSize
    }
    else {
        Write-Host "No tool history was recorded."
    }

    Write-Host "`n========== COMPLETE TRAVERSAL SEQUENCE ==========" -ForegroundColor Yellow
    if ($ToolHistory.Count -gt 0) {
        $ToolHistory |
        Select-Object step, tool, input |
        Format-Table -Wrap -AutoSize
    }

    Write-Host "`n========== PROPOSED CONCLUSIONS ==========" -ForegroundColor Yellow
    $Proposals = @(
        $ToolHistory |
        Where-Object {
            $_.tool -eq "propose_conclusion"
        }
    )

    if ($Proposals.Count -gt 0) {
        $Proposals |
        Select-Object step, input, result |
        Format-List
    }
    else {
        Write-Host "No conclusion was proposed."
    }

    Write-Host "`n========== VERIFIED SOURCE FUNCTIONS ==========" -ForegroundColor Yellow
    if ($null -ne $Result.state.read_functions) {
        $ReadFunctions = @(
            $Result.state.read_functions.PSObject.Properties |
            ForEach-Object {
                [PSCustomObject]@{
                    FunctionId    = $_.Name
                    QualifiedName = $_.Value.function.qualified_name
                    File          = $_.Value.function.relative_path
                    StartLine     = $_.Value.source.start_line
                    EndLine       = $_.Value.source.actual_end_line
                    SourceStatus  = $_.Value.status
                }
            }
        )

        if ($ReadFunctions.Count -gt 0) {
            $ReadFunctions | Format-Table -Wrap -AutoSize
            Write-Host "Total verified/read Functions:" $ReadFunctions.Count
        }
        else {
            Write-Host "No source Functions were read."
        }
    }
    else {
        Write-Host "No source Function information was recorded."
    }

    Write-Host "`n========== TOOL ERRORS OR BLOCKED CALLS ==========" -ForegroundColor Yellow
    $ToolErrors = @(
        $ToolHistory |
        Where-Object {
            $null -ne $_.result.error
        }
    )

    if ($ToolErrors.Count -gt 0) {
        $ToolErrors |
        Select-Object step, tool, input, result |
        Format-List
    }
    else {
        Write-Host "No tool errors or blocked calls were recorded."
    }

    Write-Host "`n========== REVIEW SUMMARY ==========" -ForegroundColor Yellow
    Write-Host "Status:" $Result.status
    Write-Host "Functions read:" @(
        $Result.state.read_functions.PSObject.Properties
    ).Count
    Write-Host "Tool-history entries:" $ToolHistory.Count
    Write-Host "Conclusions proposed:" $Proposals.Count
    Write-Host "Critic verdict:" $Result.critic.verdict
    Write-Host "Elapsed seconds:" $Result.execution_metrics.elapsed_seconds
    Write-Host "Bedrock calls:" $Result.execution_metrics.bedrock_calls
    Write-Host "Investigator calls:" $Result.execution_metrics.investigator_calls
    Write-Host "Critic calls:" $Result.execution_metrics.critic_calls
    Write-Host "Input tokens:" $Result.execution_metrics.input_tokens
    Write-Host "Output tokens:" $Result.execution_metrics.output_tokens
    Write-Host "Total tokens:" $Result.execution_metrics.total_tokens
    Write-Host "Estimated cost USD:" $Result.execution_metrics.estimated_cost_usd

    Write-Host "`n============================================================" -ForegroundColor Cyan
    Write-Host "                 END OF RESULT REVIEW" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan

} ".\output\investigation_result_optimized_run4_issue3.json"