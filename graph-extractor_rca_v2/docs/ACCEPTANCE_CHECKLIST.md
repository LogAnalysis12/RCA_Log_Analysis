# RCA Extractor v2 Acceptance Checklist

## Before extraction

- [ ] `python -c 'from clang import cindex; print(cindex.__file__)'` succeeds in the intended virtual environment.
- [ ] LibClang path exists and matches the Python binding version/ABI.
- [ ] `compile_commands.json` comes from the target repository/build and has valid source paths, include paths and preprocessor defines.
- [ ] `--dry-run` selects the intended C/C++ source files.
- [ ] Output directory is a new scratch location, outside the source tree and separate from any previously validated graph artifacts.

## Small smoke extraction

- [ ] Run with `--limit 1` to a new scratch output directory.
- [ ] `extraction_report.json` and `manifest.json` mark the run partial.
- [ ] Inspect the checkpoint worker log and diagnostics; confirm this source parsed under the intended compile command.
- [ ] Verify all legacy and new artifacts are present, with headers even if an artifact has zero records.
- [ ] Inspect at least one call site, read/write access, pointer/lifetime event, and relevant API/macro record from real source.

## ThreadDemo benchmark

- [ ] Run the complete extractor into a fresh scratch directory.
- [ ] Run `tools/check_graph_vs_truth.py` on the unchanged `ThreadDemo/ground_truth.json`.
- [ ] Review `FOUND` and `MISSING` rows, especially thread spawn sites, member writes/reads, `NEW`/`DELETE`, locks, and `NO_LOCK`.
- [ ] For every missing row, inspect the source and AST/worker log before editing config or extractor logic.
- [ ] Record known misses/false positives in the gap report; do not use ground truth as extractor input.

## Before downstream Neo4j loading (separate task)

- [ ] Importer maps IDs/relationships from the reviewed artifact schema.
- [ ] Importer does not persist `source_code` or source excerpts from `functions.jsonl`.
- [ ] Importer does not ingest ground-truth answers or validation reports as production evidence.
- [ ] Unresolved calls/spawns stay explicit; importer does not fabricate targets.
- [ ] `Span` remains a separate Neo4j experiment, with behavior verified by an explicit traversal test.
- [ ] Full extraction manifest is not partial and records zero failed TUs, or every failure is reviewed and documented before declaring the graph complete.
