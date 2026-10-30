"""Provider-neutral LLM backends for the traversal agent.

Primary provider:
    Amazon Bedrock Converse API

Optional future provider:
    Local OpenAI-compatible servers such as Ollama, LM Studio, vLLM,
    llama.cpp server, and text-generation-webui.

The Investigator and Critic should depend only on:
    backend.send(user_content)
    backend.send_tool_results(results)

Environment variables:
    LLM_PROVIDER       bedrock or local (default: bedrock)
    LLM_MODEL          Bedrock model/inference-profile ID or local model name
    AWS_REGION         Bedrock Region (default: us-east-1)
    BEDROCK_MAX_TOKENS Maximum output tokens (default: 2048)
    BEDROCK_TEMPERATURE Temperature (default: 0.0)

Local-only variables:
    LLM_BASE_URL       e.g. http://localhost:11434/v1
    LLM_API_KEY        optional; defaults to not-needed-for-local

No AWS credentials are stored here. Boto3 uses the normal AWS credential chain.
"""

from __future__ import annotations

from traversal.message_history import trim_message_history

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class StepResult:
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


def _bedrock_tools(canonical_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "toolSpec": {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": {"json": tool["parameters"]},
            }
        }
        for tool in canonical_tools
    ]


def _openai_tools(canonical_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in canonical_tools
    ]


def _normalize_user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, list):
        return content
    return [{"text": json.dumps(content, ensure_ascii=False, default=str)}]


class BedrockBackend:
    """Amazon Bedrock backend using the provider-neutral Converse API."""

    supports_attempt_budget_hook = True

    def __init__(
        self,
        model: str,
        canonical_tools: list[dict[str, Any]],
        region: str = "us-east-1",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        history_config: dict[str, Any] | None = None,
    ) -> None:
        import boto3
        from botocore.config import Config

        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=20,
                read_timeout=180,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
        self.model = model
        self.tools = _bedrock_tools(canonical_tools)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system = [{"text": system_prompt}] if system_prompt else None
        self.messages: list[dict[str, Any]] = []
        self.history_config = history_config or {}
        self._budget_before_attempt = None
        self._budget_after_attempt = None
        self._budget_before_request = None

    def send(self, user_content: Any) -> StepResult:
        self.messages.append(
            {"role": "user", "content": _normalize_user_content(user_content)}
        )
        return self._call()

    def send_tool_results(self, results: list[dict[str, Any]]) -> StepResult:
        content: list[dict[str, Any]] = []
        for result in results:
            tool_use_id = result.get("tool_use_id") or result.get("tool_call_id")
            if not tool_use_id:
                raise ValueError("Each tool result must contain tool_use_id.")
            raw_content = result.get("content", "")
            if isinstance(raw_content, str):
                result_content = [{"text": raw_content}]
            else:
                result_content = [
                    {
                        "json": raw_content
                        if isinstance(raw_content, (dict, list))
                        else {"value": raw_content}
                    }
                ]
            block: dict[str, Any] = {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": result_content,
                    "status": "error" if result.get("is_error") else "success",
                }
            }
            content.append(block)
        self.messages.append({"role": "user", "content": content})
        return self._call()

    def _call(self) -> StepResult:
        if self.history_config.get("enabled", True):
            self.messages = trim_message_history(
                self.messages,
                max_chars=int(self.history_config.get("max_chars", 24000)),
                keep_recent_cycles=int(self.history_config.get("keep_recent_cycles", 4)),
            )
        request: dict[str, Any] = {
            "modelId": self.model,
            "messages": self.messages,
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        if self.tools:
            request["toolConfig"] = {"tools": self.tools}
        if self.system:
            request["system"] = self.system

        before_request = getattr(self, "_budget_before_request", None)
        if before_request is not None:
            from traversal.budget import estimate_input_tokens

            before_request(estimate_input_tokens(request))
        before_attempt = getattr(self, "_budget_before_attempt", None)
        after_attempt = getattr(self, "_budget_after_attempt", None)
        if before_attempt is not None:
            before_attempt()
        try:
            response = self.client.converse(**request)
        except Exception:
            if after_attempt is not None:
                after_attempt(False)
            raise
        else:
            if after_attempt is not None:
                after_attempt(True)
        assistant_message = response["output"]["message"]
        self.messages.append(assistant_message)

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for block in assistant_message.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            if "toolUse" in block:
                tool_use = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        id=tool_use["toolUseId"],
                        name=tool_use["name"],
                        input=tool_use.get("input", {}),
                    )
                )

        raw_usage = response.get("usage", {})
        usage = {
            "input_tokens": int(raw_usage.get("inputTokens", 0)),
            "output_tokens": int(raw_usage.get("outputTokens", 0)),
            "total_tokens": int(raw_usage.get("totalTokens", 0)),
        }
        return StepResult(
            tool_calls=tool_calls,
            text="".join(text_parts),
            stop_reason=response.get("stopReason", ""),
            usage=usage,
        )


def _rate_limit_wait_seconds(error: Exception) -> float | None:
    """Extract a safe retry delay from a rate-limit response.

    Returns None when the provider gives no usable delay, or when the
    error suggests a daily quota rather than a temporary rate limit.
    """
    import re

    message = str(error)

    if re.search(
        r"requests?\s*(?:per|/)\s*day|per-day|quota.*daily|daily.*quota",
        message,
        re.IGNORECASE,
    ):
        return None

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)

    if headers:
        value = headers.get("retry-after") or headers.get("Retry-After")
        if value is not None:
            try:
                delay = float(value)
                if 0 < delay <= 60:
                    return delay
            except (TypeError, ValueError):
                pass

    match = re.search(
        r"(?:retry|try again)\s+(?:in|after)\s+"
        r"(\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|secs?|s)\b",
        message,
        re.IGNORECASE,
    )

    if not match:
        return None

    delay = float(match.group(1))
    unit = match.group(2).lower()

    if unit in ("ms", "millisecond", "milliseconds"):
        delay /= 1000.0

    if 0 < delay <= 60:
        return delay

    # Do not retry early if the provider asks for more than 60 seconds.
    return None


class LocalOpenAICompatibleBackend:
    supports_attempt_budget_hook = True
    """Optional local backend for any OpenAI-compatible chat server."""

    def __init__(
        self,
        model: str,
        canonical_tools: list[dict[str, Any]],
        base_url: str,
        api_key: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        history_config: dict[str, Any] | None = None,
        trace_recorder: Any = None,
        stage: str = "unknown",
        http_transport: Any = None,
        request_timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        import openai
        import os

        if not base_url:
            raise ValueError("LLM_BASE_URL is required when LLM_PROVIDER=local.")
        client_options: dict[str, Any] = {
            "api_key": api_key or "not-needed-for-local",
            "base_url": base_url,
            # Let our code handle 429 responses, instead of hidden SDK retries.
            "max_retries": 0,
            "timeout": max(10.0, min(180.0, float(request_timeout_seconds if request_timeout_seconds is not None else os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "90")))),
        }
        if trace_recorder is not None:
            import httpx

            client_options["http_client"] = httpx.Client(
                transport=trace_recorder.transport(
                    stage=stage,
                    model=model,
                    api_key=api_key or "not-needed-for-local",
                    inner_transport=http_transport,
                ),
                follow_redirects=True,
            )
        elif http_transport is not None:
            import httpx

            client_options["http_client"] = httpx.Client(transport=http_transport)
        self.client = openai.OpenAI(**client_options)
        self.model = model
        self.tools = _openai_tools(canonical_tools)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.messages: list[dict[str, Any]] = []
        self.history_config = history_config or {}
        self.trace_recorder = trace_recorder
        self.stage = stage
        self._last_request_started = 0.0
        self.max_retries = max(0, min(2, int(max_retries if max_retries is not None else os.environ.get("LLM_MAX_RETRIES", "2"))))
        self._budget_before_attempt = None
        self._budget_after_attempt = None
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def send(self, user_content: Any) -> StepResult:
        content = (
            user_content
            if isinstance(user_content, str)
            else json.dumps(user_content, ensure_ascii=False, default=str)
        )
        self.messages.append({"role": "user", "content": content})
        return self._call()

    def send_tool_results(self, results: list[dict[str, Any]]) -> StepResult:
        for result in results:
            tool_use_id = result.get("tool_use_id") or result.get("tool_call_id")
            if not tool_use_id:
                raise ValueError("Each tool result must contain tool_use_id.")
            content = result.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": content,
                }
            )
        return self._call()

    def _call(self) -> StepResult:
        if self.history_config.get("enabled", True):
            self.messages = trim_message_history(
                self.messages,
                max_chars=int(self.history_config.get("max_chars", 24000)),
                keep_recent_cycles=int(self.history_config.get("keep_recent_cycles", 4)),
            )
        import openai
        import os
        import time

        minimum_interval = max(
            0.0,
            float(os.environ.get("LLM_MIN_REQUEST_INTERVAL_SECONDS", "0")),
        )
        response = None
        request_payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools or None,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        before_request = getattr(self, "_budget_before_request", None)
        if before_request is not None:
            from traversal.budget import estimate_input_tokens

            estimated_tokens = estimate_input_tokens(request_payload)
            try:
                before_request(estimated_tokens)
            except Exception:
                record_rejection = getattr(self, "_budget_record_rejection", None)
                if record_rejection is not None:
                    record_rejection(
                        stage=getattr(self, "stage", "unknown"),
                        model=self.model,
                        estimated_input_tokens=estimated_tokens,
                        input_limit=getattr(self.trace_recorder, "input_limit", None),
                    )
                raise

        # Three attempts maximum: the original request plus at most two
        # retries, and only when the provider supplies a temporary wait.
        # object.__new__-constructed test doubles may not run __init__.
        max_retries = max(
            0,
            min(2, int(getattr(self, "max_retries", os.environ.get("LLM_MAX_RETRIES", "2")))),
        )
        for attempt in range(max_retries + 1):
            if minimum_interval > 0 and self._last_request_started > 0:
                elapsed = time.monotonic() - self._last_request_started
                remaining = minimum_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)

            self._last_request_started = time.monotonic()

            try:
                before_attempt = getattr(self, "_budget_before_attempt", None)
                after_attempt = getattr(self, "_budget_after_attempt", None)

                if before_attempt is not None:
                    before_attempt()
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=self.tools or None,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                    )
                except Exception:
                    if after_attempt is not None:
                        after_attempt(False)
                    raise
                else:
                    if after_attempt is not None:
                        after_attempt(True)
                break
            except openai.RateLimitError as exc:
                # Rate limits are retried only when the provider supplies
                # a safe, temporary retry delay. Daily quota failures stop.
                delay = _rate_limit_wait_seconds(exc)
                if attempt >= max_retries or delay is None:
                    raise

                time.sleep(delay)

            except openai.APIStatusError as exc:
                # Retry only transient server errors. Authentication,
                # validation, and other non-transient errors fail immediately.
                if exc.status_code not in (500, 502, 503, 504):
                    raise

                if attempt >= max_retries:
                    raise

                # Prefer a valid Retry-After header when present.
                # Otherwise use bounded exponential backoff: 1s, then 2s.
                retry_after = None
                headers = getattr(exc.response, "headers", None)
                if headers:
                    value = headers.get("retry-after")
                    if value is not None:
                        try:
                            parsed = float(value)
                            if 0 < parsed <= 30:
                                retry_after = parsed
                        except (TypeError, ValueError):
                            pass

                delay = retry_after if retry_after is not None else min(
                    2 ** attempt, 4
                )
                time.sleep(delay)

        if response is None:
            raise RuntimeError("LLM request finished without a response.")
        message = response.choices[0].message
        self.messages.append(message.model_dump(exclude_none=True))

        tool_calls: list[ToolCall] = []
        for tool_call in message.tool_calls or []:
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_invalid_json": tool_call.function.arguments}
            tool_calls.append(
                ToolCall(
                    id=tool_call.id or str(uuid.uuid4()),
                    name=tool_call.function.name,
                    input=arguments,
                )
            )

        prompt_tokens = int(getattr(response.usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(response.usage, "completion_tokens", 0) or 0)
        return StepResult(
            tool_calls=tool_calls,
            text=message.content or "",
            stop_reason=response.choices[0].finish_reason or "",
            usage={
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )


def get_backend(
    canonical_tools: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    config: dict[str, Any] | None = None,
    trace_recorder: Any = None,
    stage: str = "unknown",
    http_transport: Any = None,
):
    """Build the configured backend while keeping callers provider-neutral."""

    config = config or {}
    provider = str(
        config.get("provider") or os.getenv("LLM_PROVIDER", "bedrock")
    ).strip().lower()
    model = str(config.get("model") or os.getenv("LLM_MODEL", "")).strip()
    if not model:
        raise ValueError("Set llm.model in config.json or set LLM_MODEL.")

    max_tokens = int(
        config.get("max_tokens")
        or os.getenv("BEDROCK_MAX_TOKENS", "2048")
    )
    temperature = float(
        config.get("temperature")
        if config.get("temperature") is not None
        else os.getenv("BEDROCK_TEMPERATURE", "0.0")
    )

    if provider == "bedrock":
        region = str(
            config.get("region") or os.getenv("AWS_REGION", "us-east-1")
        )
        return BedrockBackend(
            model=model,
            canonical_tools=canonical_tools,
            region=region,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            history_config=config.get("history_config"),
        )

    if provider in {"local", "openai-compatible"}:
        base_url = str(
            config.get("base_url") or os.getenv("LLM_BASE_URL", "")
        ).strip()
        api_key = (
            config.get("api_key")
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
        )

        if not api_key:
            from pathlib import Path

            dotenv_candidates = [
                Path(__file__).resolve().parents[1] / ".env",
                Path.home() / "Desktop" / "LogAnalysis_thread" / ".env",
            ]

            for dotenv_path in dotenv_candidates:
                if not dotenv_path.is_file():
                    continue

                for raw_line in dotenv_path.read_text().splitlines():
                    line = raw_line.strip()

                    if not line or line.startswith("#") or "=" not in line:
                        continue

                    name, value = line.split("=", 1)

                    if name.strip() not in ("LLM_API_KEY", "OPENROUTER_API_KEY"):
                        continue

                    value = value.strip()

                    if (
                        len(value) >= 2
                        and value[0] == value[-1]
                        and value[0] in ("'", '"')
                    ):
                        value = value[1:-1]

                    if value:
                        api_key = value
                        break

                if api_key:
                    break

        if api_key:
            os.environ.setdefault("LLM_API_KEY", api_key)
            os.environ.setdefault("OPENROUTER_API_KEY", api_key)
        return LocalOpenAICompatibleBackend(
            model=model,
            canonical_tools=canonical_tools,
            base_url=base_url,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            history_config=config.get("history_config"),
            trace_recorder=trace_recorder,
            stage=stage,
            http_transport=http_transport,
            request_timeout_seconds=config.get("request_timeout_seconds"),
            max_retries=config.get("max_retries"),
        )

    raise ValueError(
        f"Unknown LLM provider {provider!r}. Expected 'bedrock' or 'local'."
    )
