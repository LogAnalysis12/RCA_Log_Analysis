# RCA Graph Evidence Model v2

This is the intended graph interpretation of extractor evidence. It describes an import model, not a claim that a Neo4j database has been updated.

## Node families

| Node label | Stable identity/source |
|---|---|
| `Function` | `functions.jsonl.function_id` |
| `Callable` | `callable_targets.jsonl.callable_id` (primarily lambdas). Callable-owned parameters/events use `callable_id` or `*_callable_id`; they are not Function nodes. |
| `CallSite` | `call_sites.csv.call_site_id`; one node per call/constructor occurrence |
| `ExternalSymbol` | `external_symbols.jsonl.symbol_id` |
| `Variable` | `variables.csv.variable_id` |
| `Parameter` | `function_parameters.csv.parameter_id`; may share identity with the corresponding variable record because both reference one declaration |
| `DataField` | `data_fields.csv.field_id`, plus equivalent field variables in `variables.csv` |
| `Type` | `types.csv.type_id` |
| `LifetimeEvent` | `lifetime_events.csv.event_id` |
| `PointerEvent` | `pointer_events.csv.event_id` |
| `ThreadSpawn` | `thread_spawns.csv.spawn_id` |
| `LockUsageEvent` | `lock_usage.csv.lock_event_id` |
| `SynchronizationEvent` | `synchronization_events.csv.event_id` |
| `ControlFlowEvent` | `control_flow_events.csv.event_id` |
| `MacroEvent` | `macro_events.csv.macro_event_id` |

Do not persist `source_code` or source excerpts from the legacy function artifact as graph properties. Store relative path, signature/name, identifiers and precise locations; fetch source through the controlled read-source tool when the LLM needs it.

## Relationship contract

The importer can map these artifacts to graph relationships:

| Relationship | Source artifact and certainty |
|---|---|
| `CALLS` / `CALLS_EXTERNAL` | `call_sites.csv`, attached through `CallSite`; use `caller_function_id` or `caller_callable_id` and resolved target type/ID. Preserve the per-site ID; for unresolved calls, retain the `CallSite` and resolution status instead of guessing. |
| `HAS_PARAMETER` | Function to declared parameter, from `function_parameters.csv`. |
| `HAS_TYPE` / `RETURNS_TYPE` | Variable/parameter/function to `types.csv.type_id` through `type_id` and `function_return_types.csv`. |
| `INHERITS` | `type_relationships.csv` with `relationship_type=INHERITS`. |
| `POINTS_TO_TYPE`, `REFERENCES_TYPE`, `SMART_POINTER_TO_TYPE`, `WEAKLY_REFERENCES_TYPE`, `TYPE_ALIAS_OF`, `ELEMENT_TYPE` | Type-level structural relations. `POINTS_TO_TYPE` is not an ownership claim. |
| `READS`, `WRITES`, `READS_WRITES`, `POSSIBLE_WRITE` | `variable_accesses.csv` and the existing `data_access.csv`. Preserve source location and evidence grade. |
| `PASSES_ARGUMENT` / `ARGUMENT_TO_PARAMETER` | `call_arguments.csv` plus `data_flows.csv`. Keep the argument index and call-site identity. |
| `ALIASES` / `COPIES_OWNERSHIP` / `MOVE_EXPRESSION_FLOW` | `data_flows.csv`; retain its specific `flow_kind` as a property or typed relation. These are flow candidates, not complete points-to analysis. |
| `AFFECTS` | Lifetime and pointer events to a variable/type when the target ID is non-empty. Do not fabricate a target if it is unresolved. |
| `SPAWNS_THREAD` / `DISPATCHES_ASYNC` | `thread_spawns.csv`; use `spawner_function_id` vs `spawner_callable_id` and `target_function_id` vs `target_callable_id` as distinct typed links. If the target is an external function, use `target_external_symbol_id`; if it is a function-pointer variable, link that variable and retain `target_kind`. |
| `PASSES_TO_THREAD_ENTRY` | `data_flows.csv` with `flow_kind=SPAWN_ARGUMENT_TO_PARAMETER`. This records positional evidence, not a guarantee of full alias identity. |
| `LOCK_OPERATION` / `SYNCHRONIZATION_OPERATION` | `lock_usage.csv` and `synchronization_events.csv`. Include `event`, scope and evidence grade. Do not automatically create `PROTECTS` edges to all same-function reads/writes based only on lexical overlap. |
| `HAS_CONTROL_FLOW_EVENT` | `control_flow_events.csv`, preserving condition text/line and event kind. |
| `POSSIBLY_EXPANDED_FROM` | Same-line macro association columns in lifetime/pointer events. This is explicitly a heuristic association and must retain the low-certainty provenance. |

## Modeling rules

1. Each record ID is stable within the repository/version inputs, but source movement or a changed USR may change IDs. Do not treat the IDs as eternal public identifiers.
2. Preserve individual call sites. Two calls from one function to the same target can lead to different failures and argument values. Never coerce a `Callable` ID into a `Function` field; the separate caller/spawner/target fields carry that distinction explicitly.
3. Preserve source path/line/column on event and relationship evidence. It is needed for evidence retrieval and ground-truth review.
4. Preserve unresolved evidence. An unresolved thread target is an actionable gap, not permission to guess the target.
5. Store evidence provenance as fields, including `evidence_grade`, `resolution_status`, `event_semantics`, and `interpretation` where present.
6. Separate static code evidence from runtime log/span evidence. Runtime chronology should be supplied by the log pipeline, not inferred from source ordering.
7. Keep ground-truth files, labels, expected culprit/victim, and checker outputs out of the production evidence graph and model context.
8. This schema does not specify `Span` nodes. Those were part of the previous Neo4j traversal experiment and need to remain a separate loader-side decision.
