"""Leakage-aware HMAC token derivation for RFC 0013."""

from __future__ import annotations

import hashlib
import hmac
import re

_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def protected_tokens(text: str, key: str) -> tuple[bytes, ...]:
    """Return unique, fixed-length HMAC digests without retaining plaintext terms."""
    if not key:
        raise ValueError("protected search key must not be empty")
    normalized = (match.group(0).casefold() for match in _TOKEN.finditer(text))
    return tuple(
        sorted(
            {
                hmac.new(key.encode(), token.encode(), hashlib.sha256).digest()
                for token in normalized
            }
        )
    )
