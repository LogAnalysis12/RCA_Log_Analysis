# Future multi-language adapter boundary (design only)

The current implementation parses **C and C++ only** through LibClang. Java, C#, JavaScript/TypeScript, and other language parsers are **not implemented** by this package. This document records a compatibility target so a future adapter can feed the same evidence graph without pretending LibClang parses other languages.

## Shared evidence envelope

Future adapters should emit records with the common fields wherever meaningful:

- `repository_id`, `artifact_schema_version`, `source_language`, `relative_path`
- stable `node_id` / `relationship_id`
- `line`, `column`, and end/start locations where available
- `evidence_grade`, `resolution_status`, `event_kind`
- `source_node_id`, `target_node_id`, `source_text` or `expression` when safe and needed
- parser/tool name and version in the extraction manifest

Language-specific details should be namespaced or attached as typed properties. Do not force unlike concepts into a generic column whose meaning changes by language.

## Adapter responsibilities

1. Parse using the language's own semantic frontend and project build metadata.
2. Resolve declarations and call targets where the frontend can prove the relation; emit unresolved records where it cannot.
3. Extract declarations/types, parameters/returns, call sites/arguments, reads/writes, lifetime/ownership operations, threading/async, synchronization, macro/preprocessor or generated-code provenance where applicable.
4. Emit the same kind of source/evidence provenance and clear static-vs-heuristic labels.
5. Pass neutral records to a shared normalizer/loader. The adapter must not write directly to Neo4j or encode language-specific RCA conclusions.
6. Add language-specific validation fixtures and ground truth outside the extraction source. Validate both expected evidence presence and important expected absences.

## Possible future frontends (not part of this implementation)

- C/C++: LibClang; compile database and preprocessor configuration required.
- Java: javac Trees/Elements or a suitable Java AST frontend; project build/classpath and generic/overload resolution matter.
- C#: Roslyn; solution/project references and generated sources matter.
- JavaScript/TypeScript: TypeScript Compiler API; project config, module resolution and async call graphs matter.

Do not label any of these adapters as complete until parser integration, stable IDs, output schema, negative tests and a fixture extraction have passed.
