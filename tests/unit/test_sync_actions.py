"""Unit tests for the testConnection sync-action logic (HTTP client mocked)."""

import pytest
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import ValidationResult

from sync_actions import check_anthropic_connection


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class FakeHttpClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post_raw(self, endpoint_path=None, headers=None, json=None, **kwargs):
        self.calls.append({"endpoint": endpoint_path, "headers": headers, "json": json, "kwargs": kwargs})
        return self._response


class RaisingHttpClient:
    def post_raw(self, *args, **kwargs):
        raise ConnectionError("connection refused")


def test_success_returns_validation_result():
    client = FakeHttpClient(FakeResponse(200, '{"id":"msg"}'))
    result = check_anthropic_connection("KEY_NAME_ONLY", http_client=client)
    assert isinstance(result, ValidationResult)
    # one cheap call, 1 token, haiku, key in x-api-key header
    assert client.calls[0]["endpoint"] == "/v1/messages"
    assert client.calls[0]["json"]["max_tokens"] == 1
    assert client.calls[0]["headers"]["x-api-key"] == "KEY_NAME_ONLY"


def test_401_raises_auth_error():
    client = FakeHttpClient(FakeResponse(401, "unauthorized"))
    with pytest.raises(UserException) as exc:
        check_anthropic_connection("BAD", http_client=client)
    assert "authentication" in str(exc.value).lower()


def test_other_error_raises():
    client = FakeHttpClient(FakeResponse(500, "server error"))
    with pytest.raises(UserException) as exc:
        check_anthropic_connection("KEY", http_client=client)
    assert "500" in str(exc.value)


def test_empty_key_raises():
    with pytest.raises(UserException):
        check_anthropic_connection("", http_client=FakeHttpClient(FakeResponse(200)))


def test_connection_error_becomes_user_exception():
    with pytest.raises(UserException) as exc:
        check_anthropic_connection("KEY", http_client=RaisingHttpClient())
    assert "Could not reach the Anthropic API" in str(exc.value)


def test_request_passes_explicit_timeout():
    client = FakeHttpClient(FakeResponse(200, "{}"))
    check_anthropic_connection("KEY", http_client=client)
    assert client.calls[0]["kwargs"].get("timeout") is not None
