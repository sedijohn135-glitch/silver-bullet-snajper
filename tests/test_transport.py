"""Transport-layer tests: failure classification and SDK compatibility."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

from mcp_client.transport import (
    classify_failure, describe_exception, flatten_exception, is_auth_error,
)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeHTTPStatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.response = FakeResponse(status_code)


class ConnectError(Exception):
    """Name-matched as a network failure, exactly like httpx's own type."""


def test_task_group_errors_are_unwrapped_into_something_readable():
    group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                           [FakeHTTPStatusError("Client error '401 Unauthorized'", 401)])
    described = describe_exception(group)
    assert "TaskGroup" not in described
    assert "401" in described and "FakeHTTPStatusError" in described
    assert len(flatten_exception(group)) == 1


def test_a_real_401_is_classified_as_auth():
    error = FakeHTTPStatusError("Client error '401 Unauthorized' for url ...", 401)
    assert classify_failure(error) == "auth"
    assert is_auth_error(ExceptionGroup("tg", [error]))


def test_a_proxy_refusal_is_network_not_auth():
    """A rejected CONNECT often looks like a 403; parking the bot for it is wrong."""
    error = ConnectError("connect_rejected: the egress proxy denied the CONNECT (403)")
    assert classify_failure(error) == "network"
    assert not is_auth_error(error)


def test_dropped_connections_are_network():
    for error in (ConnectError("connection reset"),
                  TimeoutError("read timed out"),
                  OSError("network unreachable")):
        assert classify_failure(error) == "network", error


def test_textual_auth_rejections_still_classify_as_auth():
    assert classify_failure(RuntimeError("invalid token")) == "auth"
    assert classify_failure(RuntimeError("Unauthorized")) == "auth"
    assert classify_failure(RuntimeError("something else entirely")) == "other"


def test_sdk_compatibility_shim_resolved_a_transport():
    from mcp_client import transport

    assert transport._CLIENT_FACTORY is not None
    assert transport._CLIENT_FACTORY.__name__ in (
        "streamable_http_client", "streamablehttp_client"
    )
