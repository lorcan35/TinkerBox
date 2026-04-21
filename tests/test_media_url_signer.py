"""Wave 14 W14-H04 regression: MediaUrlSigner produces + verifies HMAC-signed URLs.

Prior posture was "authenticated by ID obscurity" on a 48-bit uuid4
prefix — a determined attacker who landed a WS session could scrape the
stream for ids. This test pins the new signer's behaviour.
"""

import time

import pytest

from dragon_voice.media.url_signer import MediaUrlSigner


SECRET = "w14-h04-test-secret"


# ── Enabled signer ──────────────────────────────────────────────────────────


def test_sign_emits_expected_shape():
    s = MediaUrlSigner(SECRET)
    url = s.sign("abc.jpg")
    assert url.startswith("/api/media/abc.jpg?exp=")
    assert "&sig=" in url


def test_verify_accepts_freshly_signed():
    s = MediaUrlSigner(SECRET)
    url = s.sign("abc.jpg")
    # parse the query params
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    assert s.verify("abc.jpg", q["exp"][0], q["sig"][0]) is True


def test_verify_rejects_missing_sig():
    s = MediaUrlSigner(SECRET)
    exp = str(int(time.time()) + 300)
    assert s.verify("abc.jpg", exp, None) is False
    assert s.verify("abc.jpg", exp, "") is False


def test_verify_rejects_missing_exp():
    s = MediaUrlSigner(SECRET)
    assert s.verify("abc.jpg", None, "deadbeef") is False


def test_verify_rejects_wrong_secret():
    good = MediaUrlSigner(SECRET)
    url = good.sign("abc.jpg")
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    attacker = MediaUrlSigner("different-secret")
    assert attacker.verify("abc.jpg", q["exp"][0], q["sig"][0]) is False


def test_verify_rejects_tampered_media_id():
    s = MediaUrlSigner(SECRET)
    url = s.sign("abc.jpg")
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    # Same signature but asking for a different media_id → reject
    assert s.verify("other.jpg", q["exp"][0], q["sig"][0]) is False


def test_verify_rejects_expired():
    s = MediaUrlSigner(SECRET, ttl_sec=0)  # immediate expiry
    url = s.sign("abc.jpg")
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    time.sleep(1.1)
    assert s.verify("abc.jpg", q["exp"][0], q["sig"][0]) is False


def test_verify_rejects_non_numeric_exp():
    s = MediaUrlSigner(SECRET)
    # Anyone who tampered exp to a non-int should get False, not a
    # ValueError crash.
    assert s.verify("abc.jpg", "notanint", "somehex") is False


def test_constant_time_compare_used():
    """hmac.compare_digest would be trivial to replace with == in a
    future refactor; pin this to catch a timing-attack regression."""
    import dragon_voice.media.url_signer as mod
    import inspect
    src = inspect.getsource(mod.MediaUrlSigner.verify)
    assert "hmac.compare_digest" in src


# ── Disabled signer (bootstrap mode) ────────────────────────────────────────


def test_disabled_sign_returns_unsigned_url():
    s = MediaUrlSigner("")
    assert s.enabled is False
    assert s.sign("abc.jpg") == "/api/media/abc.jpg"


def test_disabled_verify_accepts_anything():
    s = MediaUrlSigner("")
    # No params — still True, to match the wave-13 C2 fail-open
    # posture for un-provisioned dev deployments.
    assert s.verify("abc.jpg", None, None) is True
    assert s.verify("abc.jpg", "0", "deadbeef") is True
