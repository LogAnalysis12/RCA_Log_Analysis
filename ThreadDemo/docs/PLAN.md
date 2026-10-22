# Phases (concurrency-aware root-cause analysis)

1. Test project + logs + answer key (this folder). DONE.
2. Extend the Clang extractor: DELETES (delete/free/reset/clear/erase), SPAWNS_THREAD (std::thread, std::async,
   CreateThread/pthread_create, thread-pool submit, with argument text), LOCKS (lock_guard/unique_lock/mutex.lock),
   and WRITES by other threads. Output new neutral files (lifetime_events.csv, thread_spawns.csv, lock_usage.csv).
   Acceptance: every @GT edge in ground_truth.json appears in the extracted graph.
3. Parse logs (existing parser) and load graph into Neo4j (loader + indexes), with parity tests.
4. Port traversal to Neo4j (graph_client, queries) and add tools: find_lifetime_events, find_thread_roots,
   find_concurrent_accessors, get_log_context, find_locks.
5. Replace Bedrock with an OpenAI-compatible provider (OpenRouter or xAI Grok) via llm.base_url / LLM_API_KEY.
6. Evaluate: run all six scenarios, score against ground_truth.json (culprit found, victim not blamed, S4 not called a race,
   S6 not called a race), compare with and without the new tools/logs.
