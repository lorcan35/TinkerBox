"""Tests for ``dragon_voice.ws_voice_admission``.

Pin every branch of the admission gate so a future refactor
can't accidentally weaken auth or break the 503 capacity
response shape.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dragon_voice.ws_voice_admission import check_ws_voice_admission


def _make_request(
    *,
    auth_header: str | None = None,
    remote: str = "192.168.1.50",
) -> MagicMock:
    req = MagicMock()
    req.headers = {}
    if auth_header is not None:
        req.headers["Authorization"] = auth_header
    req.remote = remote
    # MagicMock(spec=[]) headers wouldn't expose .get; use a real dict
    # via lambda:
    real_dict = dict(req.headers)
    req.headers = real_dict
    return req


# ─── Auth gate ────────────────────────────────────────────────


class TestAuthGate:
    def test_blank_token_allows_unauthenticated_upgrade(self):
        """When the server hasn't set an api_token (first-run /
        dev bootstrap), the upgrade proceeds — but a WARNING
        gets logged so the operator notices."""
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="",
            active_connection_count=0,
            max_connections=10,
        )
        # None means "proceed".
        assert result is None

    def test_missing_auth_header_rejected_when_token_required(self):
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is not None
        assert result.status == 401
        # JSON body shape (γ3-Dragon)
        body = result.body.decode()
        assert '"auth_failed"' in body
        assert "Invalid Dragon token" in body

    def test_wrong_bearer_token_rejected(self):
        req = _make_request(auth_header="Bearer wrong-token")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is not None
        assert result.status == 401

    def test_correct_bearer_token_passes(self):
        req = _make_request(auth_header="Bearer server-secret")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is None

    def test_non_bearer_scheme_rejected(self):
        """`Basic <…>` or any non-Bearer scheme must be rejected
        even if the token bytes happen to appear in the header."""
        req = _make_request(auth_header="Basic server-secret")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is not None
        assert result.status == 401

    def test_bearer_with_empty_token_rejected(self):
        """`Bearer ` (no value) must NOT pass even if expected
        token is blank — the auth gate doesn't run when expected
        is blank, but if it does run with `Bearer ` we must still
        reject."""
        req = _make_request(auth_header="Bearer ")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is not None
        assert result.status == 401

    def test_constant_time_compare_used(self):
        """Sanity check: even tokens with a long shared prefix
        must reject when they don't fully match.  This is a smoke
        test — the actual timing-safety lives in
        hmac.compare_digest."""
        req = _make_request(auth_header="Bearer server-secre")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=0,
            max_connections=10,
        )
        assert result is not None
        assert result.status == 401


# ─── Connection-cap gate ──────────────────────────────────────


class TestConnectionCap:
    def test_under_cap_passes(self):
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="",
            active_connection_count=4,
            max_connections=5,
        )
        assert result is None

    def test_at_cap_rejected(self):
        """Active count == max → reject (>=, not >)."""
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="",
            active_connection_count=5,
            max_connections=5,
        )
        assert result is not None
        assert result.status == 503
        body = result.body.decode()
        assert '"server_full"' in body
        assert "at capacity" in body

    def test_over_cap_rejected(self):
        """Active count > max → reject (defensive — shouldn't
        happen but we don't want to widen the breach)."""
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="",
            active_connection_count=10,
            max_connections=5,
        )
        assert result is not None
        assert result.status == 503

    def test_auth_takes_precedence_over_cap(self):
        """When BOTH gates would fire, auth (401) wins over cap
        (503).  This pins the priority order so a future refactor
        can't accidentally invert it (which would leak the
        server-busy signal to unauthenticated probes — minor
        info disclosure)."""
        req = _make_request(auth_header="Bearer wrong")
        result = check_ws_voice_admission(
            req,
            expected_token="server-secret",
            active_connection_count=999,
            max_connections=5,
        )
        assert result is not None
        assert result.status == 401  # auth, not cap


# ─── Response shape ──────────────────────────────────────────


class TestResponseShape:
    def test_auth_failed_response_is_json(self):
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="x",
            active_connection_count=0,
            max_connections=10,
        )
        assert result.headers.get("Content-Type", "").startswith(
            "application/json"
        )

    def test_server_full_response_is_json(self):
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token="",
            active_connection_count=10,
            max_connections=10,
        )
        assert result.headers.get("Content-Type", "").startswith(
            "application/json"
        )

    @pytest.mark.parametrize(
        "expected_token,count,max_,expected_code",
        [
            ("", 5, 10, None),         # blank token, under cap → pass
            ("", 10, 10, 503),         # blank token, at cap → 503
            ("t", 5, 10, 401),         # token required, no header → 401
            ("t", 10, 10, 401),        # auth wins over cap → 401
        ],
    )
    def test_priority_matrix(
        self, expected_token, count, max_, expected_code,
    ):
        req = _make_request(auth_header=None)
        result = check_ws_voice_admission(
            req,
            expected_token=expected_token,
            active_connection_count=count,
            max_connections=max_,
        )
        if expected_code is None:
            assert result is None
        else:
            assert result is not None
            assert result.status == expected_code
