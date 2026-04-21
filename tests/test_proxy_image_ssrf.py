"""Wave 14 W14-H03 regression: `MediaPipeline.proxy_image` refuses SSRF URLs.

Prompt-injection via the LLM output lets an attacker make Dragon fetch
arbitrary URLs.  Before this wave, proxy_image would happily hit:
  * AWS metadata  — `http://169.254.169.254/latest/meta-data/…`
  * Ollama        — `http://127.0.0.1:11434/api/tags`
  * TinkerClaw    — `http://localhost:18789/…`
  * RFC1918 hosts — `http://10.x.y.z/…`, `http://192.168.x.y/…`
and serve the bytes right back via the unauthenticated `/api/media/{id}`
endpoint, turning Dragon into both SSRF probe and exfil channel.

The guard lives in `dragon_voice.media.pipeline._assert_ssrf_safe_url`.
These tests pin its blocklist.  They do NOT touch the network — socket
resolution is monkey-patched.
"""

from unittest.mock import patch

import pytest

from dragon_voice.media import pipeline as pipeline_mod
from dragon_voice.media.pipeline import _assert_ssrf_safe_url, _is_public_ip


# ── _is_public_ip — cheap pin of the address-class predicate ────────────────


def test_is_public_ip_accepts_real_external():
    assert _is_public_ip("1.1.1.1") is True  # Cloudflare
    assert _is_public_ip("8.8.8.8") is True  # Google


def test_is_public_ip_rejects_loopback():
    assert _is_public_ip("127.0.0.1") is False
    assert _is_public_ip("::1") is False


def test_is_public_ip_rejects_link_local():
    assert _is_public_ip("169.254.169.254") is False  # AWS metadata
    assert _is_public_ip("fe80::1") is False


def test_is_public_ip_rejects_rfc1918():
    assert _is_public_ip("10.0.0.1") is False
    assert _is_public_ip("172.16.0.1") is False
    assert _is_public_ip("192.168.1.91") is False  # Dragon itself


def test_is_public_ip_rejects_multicast_and_reserved():
    assert _is_public_ip("224.0.0.1") is False  # multicast
    assert _is_public_ip("0.0.0.0") is False    # unspecified


# ── _assert_ssrf_safe_url — end-to-end guard including DNS resolution ───────


def _patch_dns_to(addr: str):
    """Return a getaddrinfo stub that maps any host to *addr*."""
    def _stub(host, port, *a, **kw):
        return [(None, None, None, None, (addr, 0))]
    return patch.object(pipeline_mod.socket, "getaddrinfo", _stub)


def test_assert_accepts_public_host():
    with _patch_dns_to("1.1.1.1"):
        # Should not raise
        _assert_ssrf_safe_url("https://example.com/image.jpg")


def test_assert_rejects_localhost_literal():
    # No DNS lookup needed — textual localhost aliases are pre-blocked.
    with pytest.raises(ValueError, match="localhost"):
        _assert_ssrf_safe_url("http://localhost/image.jpg")


def test_assert_rejects_aws_metadata():
    # The IP literal path — DNS not even invoked, getaddrinfo returns the
    # literal when given a numeric.
    with pytest.raises(ValueError, match="non-public"):
        _assert_ssrf_safe_url("http://169.254.169.254/latest/meta-data/")


def test_assert_rejects_rfc1918_literal():
    with pytest.raises(ValueError, match="non-public"):
        _assert_ssrf_safe_url("http://192.168.70.1/admin")


def test_assert_rejects_dns_to_loopback():
    """DNS-rebinding defence: host resolving to 127.x is rejected."""
    with _patch_dns_to("127.0.0.1"), pytest.raises(ValueError, match="non-public"):
        _assert_ssrf_safe_url("http://evil.example.com/image.jpg")


def test_assert_rejects_ftp_scheme():
    with pytest.raises(ValueError, match="non-http scheme"):
        _assert_ssrf_safe_url("ftp://example.com/image.jpg")


def test_assert_rejects_file_scheme():
    with pytest.raises(ValueError, match="non-http scheme"):
        _assert_ssrf_safe_url("file:///etc/passwd")


def test_assert_rejects_when_dns_fails():
    def _boom(*a, **kw):
        import socket as _s
        raise _s.gaierror("no such host")
    with patch.object(pipeline_mod.socket, "getaddrinfo", _boom):
        with pytest.raises(ValueError, match="DNS"):
            _assert_ssrf_safe_url("http://does-not-exist.invalid/x.jpg")
