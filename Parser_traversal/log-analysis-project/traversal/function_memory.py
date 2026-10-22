"""Small dependency-free persistent memory for verified function evidence.

Only verified, bounded metadata is stored. The module does not call an LLM and
never stores incident conclusions in reusable function memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _source_text(result: dict[str, Any]) -> str:
    source = result.get("source") or {}
    if isinstance(source, str):
        return source
    return str(source.get("numbered_source") or source.get("source") or source.get("text") or "")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


class FunctionUnderstandingMemory:
    """JSONL-backed cache keyed by repository, function ID, and source hash."""

    def __init__(self, path: str | Path, repository_id: str, *, max_records: int = 5000):
        self.path = Path(path).expanduser()
        self.repository_id = _text(repository_id, 200) or "default"
        self.max_records = max(10, int(max_records))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return latest
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    key = row.get("key")
                    if key and row.get("repository_id") == self.repository_id:
                        latest[key] = row
        except OSError:
            return latest
        return latest

    @staticmethod
    def _key(repository_id: str, function_id: str, source_hash: str) -> str:
        return _sha(f"{repository_id}\0{function_id}\0{source_hash}")

    def remember_source(self, result: dict[str, Any], *, evidence: Iterable[dict[str, Any]] = ()) -> bool:
        """Store only successful verified source reads. Returns False on invalid input."""
        if result.get("status") != "SOURCE_VERIFIED":
            return False
        function_id = _text(result.get("function_id"), 500)
        if not function_id:
            return False
        source_text = _source_text(result)
        source_hash = _sha(source_text)
        fn = result.get("function") or {}
        source = result.get("source") or {}
        if isinstance(source, str):
            source = {}
        # Store high-value metadata, not full source text.
        facts = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            fact = {
                "tool": _text(item.get("tool"), 80),
                "status": _text(item.get("status"), 80),
                "observation": _text(item.get("observation") or item.get("summary") or item.get("message"), 360),
                "verified": bool(item.get("verified", item.get("source_verified", False))),
            }
            if fact["observation"]:
                facts.append(fact)
        facts = facts[-12:]
        key = self._key(self.repository_id, function_id, source_hash)
        row = {
            "schema_version": 1,
            "key": key,
            "repository_id": self.repository_id,
            "function_id": function_id,
            "source_hash": source_hash,
            "qualified_name": _text(fn.get("qualified_name") or result.get("qualified_name"), 300),
            "relative_path": _text(fn.get("relative_path") or fn.get("file") or result.get("relative_path"), 500),
            "signature": _text(fn.get("signature") or fn.get("signature_text"), 500),
            "start_line": source.get("start_line"),
            "end_line": source.get("actual_end_line") or source.get("end_line"),
            "source_chars": len(source_text),
            "evidence": facts,
        }
        latest = self._load_latest()
        previous = latest.get(key)
        # Re-reading unchanged source must not erase previously stored graph notes.
        if previous:
            old_facts = previous.get("evidence", [])
            merged = []
            seen = set()
            for fact in list(old_facts) + list(facts):
                marker = json.dumps(fact, ensure_ascii=False, sort_keys=True)
                if marker not in seen:
                    seen.add(marker)
                    merged.append(fact)
            row["evidence"] = merged[-12:]
        if previous == row:
            return False
        # Atomic rewrite prevents partially written cache files. Keep latest rows only.
        latest[key] = row
        rows = list(latest.values())[-self.max_records:]
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in rows:
                    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass
        return True

    def get(self, function_id: str, *, source_hash: str | None = None) -> dict[str, Any] | None:
        rows = self._load_latest().values()
        matches = [r for r in rows if r.get("function_id") == function_id and
                   (source_hash is None or r.get("source_hash") == source_hash)]
        return matches[-1] if matches else None

    def render(self, function_ids: Iterable[str], *, max_chars: int = 2400, max_functions: int = 6) -> str:
        """Render bounded, verified cache entries relevant to the selected IDs."""
        wanted = list(dict.fromkeys(_text(x, 500) for x in function_ids if x))[:max_functions]
        latest = self._load_latest()
        by_function: dict[str, dict[str, Any]] = {}
        for row in latest.values():
            if row.get("function_id") in wanted:
                by_function[row["function_id"]] = row
        parts = ["REUSABLE VERIFIED FUNCTION MEMORY (source-hash keyed; not incident conclusions):"]
        for fid in wanted:
            row = by_function.get(fid)
            if not row:
                continue
            title = row.get("qualified_name") or fid
            parts.append(f"- {title} [{fid}] at {row.get('relative_path', '?')}:{row.get('start_line', '?')}-{row.get('end_line', '?')}; source_sha256={row.get('source_hash', '')[:12]}")
            for fact in row.get("evidence", [])[:4]:
                qualifier = "verified" if fact.get("verified") else "observed, unverified"
                parts.append(f"  - {qualifier} {fact.get('tool', 'evidence')}: {fact.get('observation', '')}")
        rendered = "\n".join(parts)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars].rsplit("\n", 1)[0] + "\n[Function memory clipped to configured limit.]"
        return rendered if len(parts) > 1 else ""


class ContextBuilder:
    """Build a focused memory block without making an additional model call."""
    def __init__(self, memory: FunctionUnderstandingMemory, *, max_chars: int = 2400, max_functions: int = 6):
        self.memory = memory
        self.max_chars = max(200, int(max_chars))
        self.max_functions = max(1, int(max_functions))

    def build(self, *, active_function_id: str | None = None,
              related_function_ids: Iterable[str] = ()) -> str:
        ids = ([active_function_id] if active_function_id else []) + list(related_function_ids)
        return self.memory.render(ids, max_chars=self.max_chars, max_functions=self.max_functions)
