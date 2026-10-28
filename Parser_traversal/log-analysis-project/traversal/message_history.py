"""Conservative trimming for OpenAI-compatible and Bedrock Converse histories.

Trims only complete, old tool cycles. System messages and the original user task
are retained; assistant tool calls and their matching tool results are never split.
This utility is deliberately opt-in from each backend's _call().
"""
from __future__ import annotations

import json
from typing import Any


def _chars(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, default=str))
    except Exception:
        return len(str(message))


def _tool_call_ids(message: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for call in message.get("tool_calls") or []:
        if call.get("id"):
            ids.add(str(call["id"]))
    for block in message.get("content") or []:
        use = block.get("toolUse") if isinstance(block, dict) else None
        if use and use.get("toolUseId"):
            ids.add(str(use["toolUseId"]))
    return ids


def _result_ids(message: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    if message.get("role") == "tool" and message.get("tool_call_id"):
        ids.add(str(message["tool_call_id"]))
    for block in message.get("content") or []:
        result = block.get("toolResult") if isinstance(block, dict) else None
        if result and result.get("toolUseId"):
            ids.add(str(result["toolUseId"]))
    return ids


def _is_tool_result_message(message: dict[str, Any]) -> bool:
    return bool(_result_ids(message))


def _cycles(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Return preserved headers and coherent tool cycles in chronological order."""
    headers = [m for m in messages if m.get("role") in ("system", "developer")]
    body = [m for m in messages if m.get("role") not in ("system", "developer")]
    if not body:
        return headers, []
    # Keep the first user prompt separately so the task never disappears.
    first_user_index = next((i for i, m in enumerate(body)
                             if m.get("role") == "user" and not _is_tool_result_message(m)), None)
    first_prompt: list[dict[str, Any]] = []
    if first_user_index is not None:
        first_prompt = [body[first_user_index]]
        body = body[first_user_index + 1:]
    cycles: list[list[dict[str, Any]]] = []
    i = 0
    pending: list[dict[str, Any]] = []
    while i < len(body):
        message = body[i]
        pending.append(message)
        ids = _tool_call_ids(message)
        if ids:
            remaining = set(ids)
            j = i + 1
            while j < len(body) and remaining:
                pending.append(body[j])
                remaining -= _result_ids(body[j])
                j += 1
            # Only cut at a fully matched tool result boundary.
            if not remaining:
                cycles.append(pending)
                pending = []
                i = j
                continue
            # Unmatched/unfinished tool calls must be kept intact, not trimmed.
            i = len(body)
            break
        # A completed assistant response without tool calls closes any pending turn.
        if message.get("role") == "assistant" and not ids and pending:
            cycles.append(pending)
            pending = []
        i += 1
    if pending:
        cycles.append(pending)
    return headers + first_prompt, cycles


def _compact_tool_result_messages(
    messages: list[dict[str, Any]],
    *,
    max_result_chars: int = 4000,
) -> list[dict[str, Any]]:
    """Compact oversized tool-result text without mutating input messages."""
    import copy

    result = copy.deepcopy(messages)
    limit = max(200, int(max_result_chars))
    marker = "\n...[tool result compacted for context management]..."

    def compact(text: str) -> str:
        if len(text) <= limit:
            return text
        return text[:max(1, limit - len(marker))] + marker

    for message in result:
        if message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = compact(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        block["text"] = compact(block["text"])

        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                tool_result = block.get("toolResult")
                if not isinstance(tool_result, dict):
                    continue
                items = tool_result.get("content")
                if isinstance(items, str):
                    tool_result["content"] = compact(items)
                elif isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            item["text"] = compact(item["text"])

    return result


def trim_message_history(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 18000,
    keep_recent_cycles: int = 5,
    max_result_chars: int = 4000,
) -> list[dict[str, Any]]:
    """Trim old cycles and compact large tool results while preserving tool pairing."""
    max_chars = max(2000, int(max_chars))

    # Work on a copy. Never mutate the backend's original message objects in place.
    compacted = _compact_tool_result_messages(
        messages,
        max_result_chars=max_result_chars,
    )
    headers, cycles = _cycles(compacted)

    if sum(_chars(m) for m in compacted) <= max_chars:
        return compacted

    kept = cycles[-max(1, int(keep_recent_cycles)):]
    result = list(headers)
    result.extend(m for cycle in kept for m in cycle)

    # Drop oldest complete cycles first.
    while len(kept) > 1 and sum(_chars(m) for m in result) > max_chars:
        kept.pop(0)
        result = list(headers) + [m for cycle in kept for m in cycle]

    # If a single current cycle remains oversized, preserve the compacted cycle.
    # Do not revert to the original, potentially much larger history.
    return result
