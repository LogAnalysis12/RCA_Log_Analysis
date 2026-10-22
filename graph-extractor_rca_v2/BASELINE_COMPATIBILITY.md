# Baseline compatibility record

The versioned RCA extractor delegates the four core artifacts to the existing Step 3 working extractor and preserves the existing `translation_units.csv` header. New RCA evidence is written to additional artifacts.

The original source module was not modified during this redesign:

- `extract_graph_step3_fields_working.py` SHA-256: `23221f4f4147ae9a8e3c51031c401d29f384264730fe2d74af16dc39ee014f2b`
- `extract_repository_graph_resumable.py` SHA-256 at the start of this redesign: `32638ddf09c119a8e2f3bb412911c67443f97ced20507266025616f75ac9cba0`

The latter is retained as the original runner; the new versioned runner is `extract_repository_graph_rca_v2.py`. This compatibility record is provenance, not a claim that a full AST extraction has run against the current OS/container. The current environment does not include the Python `clang.cindex` binding, so AST end-to-end validation must be run in the existing Linux environment where LibClang/clang 21 bindings are installed.
