"""Leakage-aware HMAC token derivation for RFC 0013."""

from __future__ import annotations

import hashlib
import hmac
import re

_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_DOCUMENT_MARKER = b"obelisk:protected-search:document-marker:v1"


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


def protected_document_marker(key: str) -> bytes:
    """Return a non-queryable per-key marker used only to prove index coverage."""
    if not key:
        raise ValueError("protected search key must not be empty")
    return hmac.new(key.encode(), _DOCUMENT_MARKER, hashlib.sha256).digest()


def protected_index_digests(text: str, key: str) -> tuple[bytes, ...]:
    """Return query terms plus a marker that exists even for tokenless text."""
    return tuple(sorted((*protected_tokens(text, key), protected_document_marker(key))))
