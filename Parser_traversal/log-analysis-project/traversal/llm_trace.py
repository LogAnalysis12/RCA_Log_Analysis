from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


_SECRET_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_VALUE = re.compile(r"https?://[^\s\"']+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)([^\s,;]+)"
)


def sanitize_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return "[invalid-url]"


def _provider_name(provider: str, base_url: str) -> str:
    host = (urlsplit(sanitize_base_url(base_url)).hostname or "").lower()
    if host.endswith("openrouter.ai"):
        return "openrouter"
    return provider


class LLMTraceRecorder:
    def __init__(
        self,
        output_directory: Path,
        *,
        run_id: str,
        provider: str,
        model: str,
        base_url: str,
        pricing: dict[str, Any] | None = None,
    ) -> None:
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.output_directory, 0o700)
        self.trace_path = self.output_directory / "llm_trace.jsonl"
        self.summary_path = self.output_directory / "llm_trace_summary.json"
        self.run_id = run_id
        self.provider = _provider_name(provider, base_url)
        self.api_compatibility = provider
        self.model = model
        self.base_url = sanitize_base_url(base_url)
        self.pricing = pricing or {}
        self.input_rate = self._pricing_rate("input_per_million_tokens_usd")
        self.output_rate = self._pricing_rate("output_per_million_tokens_usd")
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._secrets: list[str] = []
        self._next_call_index = 0
        descriptor = os.open(
            self.trace_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        self._descriptor = descriptor

    def _pricing_rate(self, key: str) -> float | None:
        try:
            return float(self.pricing[key])
        except (KeyError, TypeError, ValueError):
            return None

    def register_secret(self, value: str | None) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def begin_call(self) -> tuple[int, str, float]:
        with self._lock:
            self._next_call_index += 1
            call_index = self._next_call_index
        return (
            call_index,
            datetime.now(timezone.utc).isoformat(),
            time.perf_counter(),
        )

    def _sanitize_text(self, value: str) -> str:
        sanitized = value
        for secret in self._secrets:
            sanitized = sanitized.replace(secret, "[REDACTED]")
        sanitized = _URL_VALUE.sub(
            lambda match: sanitize_base_url(match.group(0)),
            sanitized,
        )
        sanitized = _BEARER_VALUE.sub(r"\1[REDACTED]", sanitized)
        return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", sanitized)

    def _sanitize(self, value: Any, key: str | None = None) -> Any:
        if key and _SECRET_KEYS.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): self._sanitize(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return self._sanitize_text(value)
        return value

    def _parse_payload(self, body: bytes) -> tuple[Any, str]:
        text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None, self._sanitize_text(text)
        sanitized = self._sanitize(payload)
        sanitized_text = text if sanitized == payload else json.dumps(sanitized, ensure_ascii=False)
        return sanitized, sanitized_text

    @staticmethod
    def _reported_usage(payload: Any) -> dict[str, int | None] | None:
        if not isinstance(payload, dict):
            return None
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None

        def token_count(*names: str) -> int | None:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
            return None

        return {
            "input_tokens": token_count("prompt_tokens", "input_tokens"),
            "output_tokens": token_count("completion_tokens", "output_tokens"),
            "total_tokens": token_count("total_tokens"),
        }

    def _cost(self, usage: dict[str, int | None] | None) -> dict[str, Any]:
        input_tokens = usage.get("input_tokens") if usage else None
        output_tokens = usage.get("output_tokens") if usage else None
        input_cost = (
            input_tokens * self.input_rate / 1_000_000
            if input_tokens is not None and self.input_rate is not None
            else None
        )
        output_cost = (
            output_tokens * self.output_rate / 1_000_000
            if output_tokens is not None and self.output_rate is not None
            else None
        )
        total_cost = (
            input_cost + output_cost
            if input_cost is not None and output_cost is not None
            else None
        )
        return {
            "basis": "estimated_from_provider_usage_and_configured_pricing",
            "input_rate_usd_per_million_tokens": self.input_rate,
            "output_rate_usd_per_million_tokens": self.output_rate,
            "estimated_input_cost_usd": input_cost,
            "estimated_output_cost_usd": output_cost,
            "estimated_total_cost_usd": total_cost,
        }

    def record_attempt(
        self,
        *,
        call_index: int,
        timestamp: str,
        started: float,
        stage: str,
        model: str,
        request_method: str,
        request_body: bytes,
        status_code: int | None = None,
        response_headers: Any = None,
        response_body: bytes | None = None,
        error: Exception | None = None,
    ) -> None:
        request_payload, serialized_request = self._parse_payload(request_body)
        response_payload = None
        serialized_response = None
        if response_body is not None:
            response_payload, serialized_response = self._parse_payload(response_body)

        usage = self._reported_usage(response_payload)
        choices = response_payload.get("choices", []) if isinstance(response_payload, dict) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        assistant_message = choice.get("message") if isinstance(choice, dict) else None
        response_id = response_payload.get("id") if isinstance(response_payload, dict) else None
        request_id = response_headers.get("x-request-id") if response_headers is not None else None
        response_error = response_payload.get("error") if isinstance(response_payload, dict) else None
        if error is not None:
            response_error = {
                "type": type(error).__name__,
                "message": self._sanitize_text(str(error)),
            }

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        record = {
            "run_id": self.run_id,
            "call_index": call_index,
            "timestamp": timestamp,
            "provider": self.provider,
            "api_compatibility": self.api_compatibility,
            "model": model,
            "base_url": self.base_url,
            "stage": stage,
            "request_id": request_id,
            "request": {
                "method": request_method,
                "model": request_payload.get("model", model) if isinstance(request_payload, dict) else model,
                "payload": request_payload,
                "serialized_body": serialized_request,
            },
            "response": {
                "http_status": status_code,
                "provider_response_id": response_id,
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "assistant_message": self._sanitize(assistant_message),
                "payload": response_payload,
                "serialized_body": serialized_response,
                "error": self._sanitize(response_error),
            },
            "usage": {
                "source": "provider_reported" if usage else "unavailable",
                "provider_reported": usage,
            },
            "duration_ms": duration_ms,
            "success": error is None and status_code is not None and 200 <= status_code < 300,
            "cost": self._cost(usage),
        }
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)
            try:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                os.write(self._descriptor, line.encode("utf-8"))
            except OSError:
                pass

    def transport(
        self,
        *,
        stage: str,
        model: str,
        api_key: str | None,
        inner_transport: Any = None,
    ):
        import httpx

        self.register_secret(api_key)
        inner = inner_transport or httpx.HTTPTransport(trust_env=True)
        return _TracingTransport(
            inner,
            self,
            stage=stage,
            model=model,
        )

    def write_summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)
        successful = [record for record in records if record["success"]]
        failed = [record for record in records if not record["success"]]
        usages = [record["usage"]["provider_reported"] for record in records]
        available_input = [item["input_tokens"] for item in usages if item and item["input_tokens"] is not None]
        available_output = [item["output_tokens"] for item in usages if item and item["output_tokens"] is not None]
        available_total = [item["total_tokens"] for item in usages if item and item["total_tokens"] is not None]
        total_input = sum(available_input)
        total_output = sum(available_output)
        total_tokens = total_input + total_output if available_input or available_output else None
        total_costs = [record["cost"]["estimated_total_cost_usd"] for record in records]
        known_costs = [cost for cost in total_costs if cost is not None]
        summary = {
            "run_id": self.run_id,
            "provider": self.provider,
            "api_compatibility": self.api_compatibility,
            "model": self.model,
            "base_url": self.base_url,
            "actual_api_calls": len(records),
            "successful_calls": len(successful),
            "failed_calls": len(failed),
            "total_input_tokens": total_input if available_input else None,
            "total_output_tokens": total_output if available_output else None,
            "total_tokens": total_tokens,
            "provider_reported_total_tokens": sum(available_total) if available_total else None,
            "token_usage_basis": "provider-reported input/output counts; total_tokens is their sum",
            "usage_unavailable_call_indexes": [
                record["call_index"] for record in records
                if not record["usage"]["provider_reported"]
                or record["usage"]["provider_reported"]["input_tokens"] is None
                or record["usage"]["provider_reported"]["output_tokens"] is None
            ],
            "estimated_total_cost_usd": sum(known_costs) if known_costs and len(known_costs) == len(records) else None,
            "cost_unavailable_call_indexes": [
                record["call_index"] for record in records
                if record["cost"]["estimated_total_cost_usd"] is None
            ],
            "total_model_call_duration_ms": round(sum(record["duration_ms"] for record in records), 3),
            "per_call": [
                {
                    "call_index": record["call_index"],
                    "stage": record["stage"],
                    "model": record["model"],
                    "success": record["success"],
                    "http_status": record["response"]["http_status"],
                    "duration_ms": record["duration_ms"],
                    "provider_reported_usage": record["usage"]["provider_reported"],
                    "estimated_total_cost_usd": record["cost"]["estimated_total_cost_usd"],
                    "request_id": record["request_id"],
                    "provider_response_id": record["response"]["provider_response_id"],
                    "error": record["response"]["error"],
                }
                for record in records
            ],
        }
        try:
            self.summary_path.write_text(
                json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.summary_path, 0o600)
        except OSError:
            pass
        return summary

    def write_result_copy(self, result: dict[str, Any]) -> None:
        result_path = self.output_directory / "rca_result.json"
        try:
            result_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            os.chmod(result_path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        try:
            os.close(self._descriptor)
        except OSError:
            pass


class _TracingTransport(httpx.BaseTransport):
    def __init__(self, inner: Any, recorder: LLMTraceRecorder, *, stage: str, model: str):
        self.inner = inner
        self.recorder = recorder
        self.stage = stage
        self.model = model

    def handle_request(self, request: Any) -> Any:
        call_index, timestamp, started = self.recorder.begin_call()
        try:
            request_body = request.read()
        except Exception:
            request_body = b""
        try:
            response = self.inner.handle_request(request)
        except Exception as error:
            self.recorder.record_attempt(
                call_index=call_index,
                timestamp=timestamp,
                started=started,
                stage=self.stage,
                model=self.model,
                request_method=request.method,
                request_body=request_body,
                error=error,
            )
            raise

        try:
            response_body = response.read()
        except Exception as error:
            response_body = b""
            self.recorder.record_attempt(
                call_index=call_index,
                timestamp=timestamp,
                started=started,
                stage=self.stage,
                model=self.model,
                request_method=request.method,
                request_body=request_body,
                status_code=response.status_code,
                response_headers=response.headers,
                response_body=response_body,
                error=error,
            )
            return response

        self.recorder.record_attempt(
            call_index=call_index,
            timestamp=timestamp,
            started=started,
            stage=self.stage,
            model=self.model,
            request_method=request.method,
            request_body=request_body,
            status_code=response.status_code,
            response_headers=response.headers,
            response_body=response_body,
        )
        return response

    def close(self) -> None:
        self.inner.close()