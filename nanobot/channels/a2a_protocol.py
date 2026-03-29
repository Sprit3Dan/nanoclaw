"""A2A protocol helpers: envelope validation, signing, and URL utilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, MutableSet
from urllib.parse import urlsplit

PROTOCOL = "nanobot-a2a/v1"
AUTH_METHOD = "hmac-sha256"
MESSAGE_TYPE_DELEGATION_REQUEST = "delegation_request"


@dataclass(slots=True)
class A2AEnvelopePolicy:
    """Validation/signing policy for inbound A2A envelopes."""

    agent_id: str
    shared_secret: str = ""
    require_auth: bool = False
    clock_skew_seconds: int = 300
    protocol: str = PROTOCOL
    auth_method: str = AUTH_METHOD


def canonical_json(data: Mapping[str, Any]) -> str:
    """Encode JSON in canonical form suitable for HMAC signing."""
    return json.dumps(dict(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def join_url(base: str, path: str) -> str:
    """Join base URL and path safely without duplicate slashes."""
    b = (base or "").rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return f"{b}{p}"


def normalize_base_url(value: str) -> str | None:
    """Normalize/validate absolute http(s) base URL."""
    v = (value or "").strip().rstrip("/")
    if not v:
        return None
    try:
        parsed = urlsplit(v)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return v


def should_sign(shared_secret: str) -> bool:
    """Whether outbound envelopes should include auth."""
    return bool((shared_secret or "").strip())


def sign_envelope(envelope: Mapping[str, Any], shared_secret: str) -> str:
    """Compute HMAC signature for envelope payload."""
    body = dict(envelope)
    body.pop("auth", None)
    payload = canonical_json(body).encode("utf-8")
    return hmac.new(shared_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def make_auth(
    envelope: Mapping[str, Any],
    shared_secret: str,
    *,
    auth_method: str = AUTH_METHOD,
) -> dict[str, str]:
    """Build auth object for outbound envelope."""
    return {
        "method": auth_method,
        "signature": sign_envelope(envelope, shared_secret),
    }


def verify_auth(
    envelope: Mapping[str, Any],
    shared_secret: str,
    *,
    auth_method: str = AUTH_METHOD,
) -> tuple[bool, str | None]:
    """Verify auth signature on envelope."""
    auth = envelope.get("auth")
    if not isinstance(auth, Mapping):
        return False, "invalid auth object"

    method = auth.get("method")
    signature = auth.get("signature")
    if method != auth_method:
        return False, f"unsupported auth method: {method}"
    if not isinstance(signature, str) or not signature:
        return False, "missing signature"

    expected = sign_envelope(envelope, shared_secret)
    if not hmac.compare_digest(signature, expected):
        return False, "signature mismatch"
    return True, None


def validate_inbound_envelope(
    envelope: Mapping[str, Any],
    *,
    policy: A2AEnvelopePolicy,
    seen_nonces: MutableMapping[str, float] | MutableSet[str] | None = None,
    now_ts: int | None = None,
) -> tuple[bool, str | None]:
    """
    Validate inbound A2A envelope against protocol contract.

    If `seen_nonces` is provided, this function also performs replay checks and
    records the nonce on success.
    """
    protocol = envelope.get("protocol")
    if protocol != policy.protocol:
        return False, f"unsupported protocol: {protocol}"

    to_agent = envelope.get("to_agent")
    if to_agent not in (None, "", policy.agent_id, "*"):
        return False, f"wrong recipient: {to_agent}"

    from_agent = envelope.get("from_agent")
    if not isinstance(from_agent, str) or not from_agent:
        return False, "missing from_agent"

    message_type = envelope.get("message_type")
    if message_type != MESSAGE_TYPE_DELEGATION_REQUEST:
        return False, f"unsupported message_type: {message_type}"

    correlation_id = envelope.get("correlation_id")
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        return False, "missing correlation_id"

    timestamp = envelope.get("timestamp")
    if not isinstance(timestamp, int):
        return False, "missing or invalid timestamp"

    current = int(time.time()) if now_ts is None else int(now_ts)
    skew = abs(current - timestamp)
    if skew > int(policy.clock_skew_seconds):
        return False, f"timestamp skew too large ({skew}s)"

    nonce = envelope.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return False, "missing nonce"

    if seen_nonces is not None:
        if nonce in seen_nonces:
            return False, "replay detected (nonce already seen)"
        if isinstance(seen_nonces, MutableMapping):
            seen_nonces[nonce] = float(current)
        else:
            seen_nonces.add(nonce)

    has_auth = isinstance(envelope.get("auth"), Mapping)
    if policy.require_auth and not has_auth:
        return False, "authentication required"

    if has_auth:
        if not policy.shared_secret:
            return False, "auth provided but shared_secret is not configured"
        ok, err = verify_auth(envelope, policy.shared_secret, auth_method=policy.auth_method)
        if not ok:
            return False, err

    return True, None
