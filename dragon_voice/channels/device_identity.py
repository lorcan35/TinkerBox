"""Ed25519 device identity for the OpenClaw gateway handshake (W7-F.4).

Without a device identity in the connect frame, the gateway clears the
client's self-declared scopes (see
``openclaw/src/gateway/server/ws-connection/message-handler.ts:L510-512``),
which made every ``send`` RPC fail with ``missing scope: operator.write``
after W7-F.3 fixed the schema-level connect rejections.

This module mirrors the OpenClaw TypeScript helpers in
``openclaw/src/infra/device-identity.ts`` so the gateway's signature
verifier accepts our connect frame.  Specifically:

  * ed25519 keypair, persisted as PEM in
    ``~/.dragon/identity/device.json`` (mode 0o600).
  * ``device_id = sha256(raw_public_key_bytes).hexdigest()`` — matches
    ``deriveDeviceIdFromPublicKey``.
  * ``public_key_b64url`` = base64url(no padding) of the raw 32-byte
    ed25519 public key extracted from the SPKI PEM — matches
    ``publicKeyRawBase64UrlFromPem``.
  * ``sign_payload`` = base64url(no padding) of the 64-byte ed25519
    signature over the UTF-8 payload — matches ``signDevicePayload``.
  * ``build_v3_payload`` = pipe-delimited canonical string identical to
    ``buildDeviceAuthPayloadV3``.

Once these match byte-for-byte, the gateway's loopback "skip backend
self-pairing" rule (``shouldSkipBackendSelfPairing`` in
``handshake-auth-helpers.ts``) admits us as a paired peer for
``gateway-client`` + ``backend`` mode + token auth, without requiring
operator approval through the pairing UI.  The device identity is then
the cryptographic binding that lets the gateway grant the requested
scopes.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# The 12-byte DER SPKI prefix that wraps a raw ed25519 public key.  Used
# to strip the prefix when extracting the 32-byte raw key from a SPKI
# encoding, matching the OpenClaw helper's algorithm exactly.
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")

# Storage version — bump only on incompatible on-disk format changes.
_STORED_FORMAT_VERSION = 1


def _default_identity_path() -> str:
    """Default location for the persisted device identity.

    Lives outside the repo (``~/.dragon/identity/device.json``) so a
    ``git pull`` or container redeploy doesn't clobber it — losing the
    keypair would force a re-pair with the gateway.
    """
    home = os.path.expanduser("~")
    return os.path.join(home, ".dragon", "identity", "device.json")


def _b64url_encode(data: bytes) -> str:
    """Base64url encoding without padding — matches OpenClaw's TS helper."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _derive_raw_public_key(public_key_pem: str) -> bytes:
    """Return the 32-byte raw ed25519 public key from a SPKI PEM string."""
    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("public key is not ed25519")
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _fingerprint_public_key(public_key_pem: str) -> str:
    return hashlib.sha256(_derive_raw_public_key(public_key_pem)).hexdigest()


def _normalize_metadata(value: Optional[str]) -> str:
    """ASCII-lowercase + trim, matching ``normalizeDeviceMetadataForAuth``."""
    if value is None:
        return ""
    trimmed = value.strip()
    if not trimmed:
        return ""
    # Only ASCII letters get lowercased on the TS side to keep the
    # canonical form deterministic across runtimes.  ``str.lower()`` in
    # Python applies Unicode lowercasing too — close enough for the
    # ASCII metadata fields we actually emit (linux, x86_64, etc.).
    return trimmed.lower()


@dataclasses.dataclass(frozen=True)
class DeviceIdentity:
    """An ed25519 keypair + the sha256-derived device id.

    Construct via :func:`load_or_create_identity` rather than manually —
    the persistence + permission handling lives there.
    """

    device_id: str
    public_key_pem: str
    private_key_pem: str

    def public_key_b64url(self) -> str:
        """Raw ed25519 public key, base64url-encoded (no padding)."""
        return _b64url_encode(_derive_raw_public_key(self.public_key_pem))

    def sign(self, payload: str) -> str:
        """Sign ``payload`` (UTF-8) and return base64url(no-padding) signature."""
        key = serialization.load_pem_private_key(
            self.private_key_pem.encode("ascii"), password=None,
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private key is not ed25519")
        sig = key.sign(payload.encode("utf-8"))
        return _b64url_encode(sig)


def build_v3_payload(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: Optional[str],
    nonce: str,
    platform: Optional[str] = None,
    device_family: Optional[str] = None,
) -> str:
    """Canonical v3 payload string — pipe-delimited, joined-comma scopes.

    Mirrors ``buildDeviceAuthPayloadV3`` in
    ``openclaw/src/gateway/device-auth.ts``.  The gateway verifies the
    signature against this exact string, so every component must be
    byte-identical to the TS implementation: comma-joined scopes (no
    spaces), millisecond timestamp as decimal string, empty-string
    fallback for ``token`` when None, lower-ascii metadata.
    """
    return "|".join(
        [
            "v3",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
            _normalize_metadata(platform),
            _normalize_metadata(device_family),
        ]
    )


def load_or_create_identity(
    path: Optional[str] = None,
) -> DeviceIdentity:
    """Load the persisted identity, or generate + save a fresh one.

    The keypair is regenerated only on first call (or after the on-disk
    file is deleted) — subsequent calls return the same identity so the
    gateway's pairing record stays valid across Dragon restarts.

    Permissions: the JSON file is written with mode 0o600 since it
    contains the ed25519 private key.  ``chmod`` is best-effort — if it
    fails (e.g. NFS share), the file write itself still succeeds.

    If the on-disk file's stored ``device_id`` no longer matches the
    sha256 of its public key, the record is rewritten with the
    fingerprint-derived id.  This guards against accidental edits.
    """
    file_path = path or _default_identity_path()
    try:
        with open(file_path) as f:
            stored = json.load(f)
        if (
            isinstance(stored, dict)
            and stored.get("version") == _STORED_FORMAT_VERSION
            and isinstance(stored.get("deviceId"), str)
            and isinstance(stored.get("publicKeyPem"), str)
            and isinstance(stored.get("privateKeyPem"), str)
        ):
            public_pem = stored["publicKeyPem"]
            stored_id = stored["deviceId"]
            derived_id = _fingerprint_public_key(public_pem)
            if derived_id != stored_id:
                logger.warning(
                    "device_identity: stored deviceId %s != sha256(publicKey) %s — rewriting",
                    stored_id[:8], derived_id[:8],
                )
                _write_identity(
                    file_path,
                    DeviceIdentity(
                        device_id=derived_id,
                        public_key_pem=public_pem,
                        private_key_pem=stored["privateKeyPem"],
                    ),
                )
                stored_id = derived_id
            return DeviceIdentity(
                device_id=stored_id,
                public_key_pem=public_pem,
                private_key_pem=stored["privateKeyPem"],
            )
    except (OSError, ValueError, json.JSONDecodeError):
        # Fall through to regenerate.  Malformed records are replaced
        # rather than blocking boot — losing an identity matters only if
        # the gateway has a paired record for it (in which case
        # re-pairing is the recovery path).
        pass

    identity = _generate_identity()
    _write_identity(file_path, identity)
    logger.info(
        "device_identity: generated fresh ed25519 keypair, device_id=%s",
        identity.device_id[:12],
    )
    return identity


def _generate_identity() -> DeviceIdentity:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_key_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    device_id = _fingerprint_public_key(public_key_pem)
    return DeviceIdentity(
        device_id=device_id,
        public_key_pem=public_key_pem,
        private_key_pem=private_key_pem,
    )


def _write_identity(path: str, identity: DeviceIdentity) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": _STORED_FORMAT_VERSION,
        "deviceId": identity.device_id,
        "publicKeyPem": identity.public_key_pem,
        "privateKeyPem": identity.private_key_pem,
    }
    # Write to a temp path + rename for atomic replacement so a crash
    # mid-write doesn't leave a half-written identity file.
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        # best-effort — keep going even if chmod fails (e.g. NFS).
        pass
    os.replace(tmp_path, path)
