"""API-key authentication. Only SHA-256 digests of keys are configured; keys never reach disk."""

from __future__ import annotations

import hashlib
import hmac

from .config import Team


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def authenticate(authorization: str | None, teams: tuple[Team, ...]) -> Team | None:
    """The team owning the presented key, or None. Every digest is compared in constant time."""
    token = bearer_token(authorization)
    if token is None:
        return None
    presented = digest(token)
    match = None
    for team in teams:  # no early exit: response time does not depend on which team matched
        if hmac.compare_digest(presented, team.key_sha256):
            match = team
    return match
