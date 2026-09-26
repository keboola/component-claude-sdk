"""Unit tests for the testConnection sync-action logic (HTTP client mocked)."""

import pytest
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import ValidationResult

from sync_actions import check_anthropic_connection, list_github_repos


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


def test_403_raises_auth_error():
    """403 is the sibling of 401 in the auth branch — it must also raise."""
    client = FakeHttpClient(FakeResponse(403, "forbidden"))
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


class FakeGetResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []

    def json(self):
        return self._json_body


class FakePaginatedHttpClient:
    """Serves one page of repos per call, based on the 'page' param."""

    def __init__(self, pages: dict[int, list[dict]], status_code: int = 200):
        self._pages = pages
        self._status_code = status_code
        self.calls = []

    def get_raw(self, endpoint_path=None, headers=None, params=None, **kwargs):
        self.calls.append({"endpoint": endpoint_path, "headers": headers, "params": params, "kwargs": kwargs})
        page = params["page"]
        return FakeGetResponse(self._status_code, self._pages.get(page, []))


class RaisingGetHttpClient:
    def get_raw(self, *args, **kwargs):
        raise ConnectionError("connection refused")


def test_list_github_repos_empty_token_raises():
    with pytest.raises(UserException):
        list_github_repos("", http_client=FakePaginatedHttpClient({}))


def test_list_github_repos_single_page():
    client = FakePaginatedHttpClient({1: [{"full_name": "acme/widgets"}, {"full_name": "acme/gadgets"}]})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert [r.value for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert [r.label for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert client.calls[0]["headers"]["Authorization"] == "Bearer TOKEN_NAME_ONLY"


def test_list_github_repos_paginates_fully():
    """A full first page (100 entries) triggers a second page fetch; a short page stops."""
    page_1 = [{"full_name": f"acme/repo-{i}"} for i in range(100)]
    page_2 = [{"full_name": "acme/repo-100"}]
    client = FakePaginatedHttpClient({1: page_1, 2: page_2})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert len(repos) == 101
    assert repos[-1].value == "acme/repo-100"
    assert len(client.calls) == 2


def test_list_github_repos_stops_on_empty_page():
    client = FakePaginatedHttpClient({1: []})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert repos == []
    assert len(client.calls) == 1


def test_list_github_repos_401_raises_auth_error():
    client = FakePaginatedHttpClient({1: []}, status_code=401)
    with pytest.raises(UserException) as exc:
        list_github_repos("BAD_TOKEN", http_client=client)
    assert "authentication" in str(exc.value).lower()


def test_list_github_repos_403_raises_auth_error():
    client = FakePaginatedHttpClient({1: []}, status_code=403)
    with pytest.raises(UserException) as exc:
        list_github_repos("BAD_TOKEN", http_client=client)
    assert "authentication" in str(exc.value).lower()


def test_list_github_repos_other_error_raises():
    client = FakePaginatedHttpClient({1: []}, status_code=500)
    with pytest.raises(UserException) as exc:
        list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert "500" in str(exc.value)


def test_list_github_repos_connection_error_becomes_user_exception():
    with pytest.raises(UserException) as exc:
        list_github_repos("TOKEN_NAME_ONLY", http_client=RaisingGetHttpClient())
    assert "Could not reach" in str(exc.value) or "GitHub" in str(exc.value)
