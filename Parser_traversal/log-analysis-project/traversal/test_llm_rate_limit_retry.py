from traversal.llm_backends import _rate_limit_wait_seconds


def test_parses_gemini_retry_delay():
    error = RuntimeError(
        "Generate requests per minute limit exceeded. "
        "Please retry in 20.962721007s."
    )
    assert abs(_rate_limit_wait_seconds(error) - 20.962721007) < 0.001


def test_parses_millisecond_retry_delay():
    error = RuntimeError("Rate limit reached. Please retry in 750ms.")
    assert _rate_limit_wait_seconds(error) == 0.75


def test_returns_none_when_retry_delay_is_missing():
    error = RuntimeError("Rate limit exceeded. Check your account.")
    assert _rate_limit_wait_seconds(error) is None


def test_does_not_retry_daily_quota_errors():
    error = RuntimeError(
        "Daily quota exceeded for requests per day. Please retry in 30s."
    )
    assert _rate_limit_wait_seconds(error) is None


def test_does_not_retry_when_delay_exceeds_sixty_seconds():
    error = RuntimeError("Rate limit reached. Please retry in 120s.")
    assert _rate_limit_wait_seconds(error) is None


def test_backend_retries_temporary_rate_limits(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    import openai

    from traversal.llm_backends import LocalOpenAICompatibleBackend

    monkeypatch.setenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "0")

    def rate_limit_error():
        message = "Rate limit exceeded. Please retry in 0.01s."
        response = httpx.Response(
            429,
            request=httpx.Request(
                "POST", "https://example.test/v1/chat/completions"
            ),
            json={"error": {"message": message}},
        )
        return openai.RateLimitError(
            message, response=response, body={"error": {"message": message}}
        )

    class FakeMessage:
        content = "ok"
        tool_calls = []

        def model_dump(self, exclude_none=True):
            return {"role": "assistant", "content": "ok"}

    success = SimpleNamespace(
        choices=[
            SimpleNamespace(message=FakeMessage(), finish_reason="stop")
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
    )

    create = Mock(
        side_effect=[rate_limit_error(), rate_limit_error(), success]
    )

    backend = object.__new__(LocalOpenAICompatibleBackend)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    backend.model = "mock-model"
    backend.tools = []
    backend.max_tokens = 32
    backend.temperature = 0
    backend.messages = [{"role": "user", "content": "test"}]
    backend.history_config = {"enabled": False}
    backend._last_request_started = 0.0

    sleeps = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    result = backend._call()

    assert create.call_count == 3
    assert len(sleeps) == 2
    assert all(0 < delay <= 0.02 for delay in sleeps)
    assert result.text == "ok"


def test_backend_does_not_retry_rate_limit_without_delay(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    import openai
    import pytest

    from traversal.llm_backends import LocalOpenAICompatibleBackend

    monkeypatch.setenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "0")

    message = "Rate limit exceeded. Check quota and billing."
    response = httpx.Response(
        429,
        request=httpx.Request(
            "POST", "https://example.test/v1/chat/completions"
        ),
        json={"error": {"message": message}},
    )
    error = openai.RateLimitError(
        message, response=response, body={"error": {"message": message}}
    )

    create = Mock(side_effect=error)

    backend = object.__new__(LocalOpenAICompatibleBackend)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    backend.model = "mock-model"
    backend.tools = []
    backend.max_tokens = 32
    backend.temperature = 0
    backend.messages = [{"role": "user", "content": "test"}]
    backend.history_config = {"enabled": False}
    backend._last_request_started = 0.0

    with pytest.raises(openai.RateLimitError):
        backend._call()

    assert create.call_count == 1


def test_backend_retries_transient_503(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx

    from traversal.llm_backends import LocalOpenAICompatibleBackend

    monkeypatch.setenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "0")

    def server_error():
        response = httpx.Response(
            503,
            request=httpx.Request(
                "POST", "https://example.test/v1/chat/completions"
            ),
            json={
                "error": {
                    "message": "This model is currently experiencing high demand."
                }
            },
        )
        import openai
        return openai.InternalServerError(
            "Service unavailable",
            response=response,
            body={"error": {"message": "This model is currently experiencing high demand."}},
        )

    class FakeMessage:
        content = "ok"
        tool_calls = []

        def model_dump(self, exclude_none=True):
            return {"role": "assistant", "content": "ok"}

    success = SimpleNamespace(
        choices=[
            SimpleNamespace(message=FakeMessage(), finish_reason="stop")
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
    )

    create = Mock(side_effect=[server_error(), server_error(), success])

    backend = object.__new__(LocalOpenAICompatibleBackend)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    backend.model = "mock-model"
    backend.tools = []
    backend.max_tokens = 32
    backend.temperature = 0
    backend.messages = [{"role": "user", "content": "test"}]
    backend.history_config = {"enabled": False}
    backend._last_request_started = 0.0

    sleeps = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    result = backend._call()

    assert result.text == "ok"
    assert create.call_count == 3
    assert sleeps == [1, 2]


def test_backend_does_not_retry_non_transient_status(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    import openai
    import pytest

    from traversal.llm_backends import LocalOpenAICompatibleBackend

    monkeypatch.setenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "0")

    response = httpx.Response(
        400,
        request=httpx.Request(
            "POST", "https://example.test/v1/chat/completions"
        ),
        json={"error": {"message": "Invalid request"}},
    )
    error = openai.BadRequestError(
        "Invalid request",
        response=response,
        body={"error": {"message": "Invalid request"}},
    )

    create = Mock(side_effect=error)

    backend = object.__new__(LocalOpenAICompatibleBackend)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    backend.model = "mock-model"
    backend.tools = []
    backend.max_tokens = 32
    backend.temperature = 0
    backend.messages = [{"role": "user", "content": "test"}]
    backend.history_config = {"enabled": False}
    backend._last_request_started = 0.0

    with pytest.raises(openai.BadRequestError):
        backend._call()

    assert create.call_count == 1
