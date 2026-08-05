"""Signed, purpose-scoped tokens for no-login email actions.

Under single opt-in there is no confirmation link, so unsubscribe is the only
token most hosts will mint. It still has to be right: it is the sole way off a
list, and CAN-SPAM plus the Gmail/Yahoo bulk-sender rules make a working
one-click opt-out mandatory rather than courteous.

Three design points, each of which has been got wrong somewhere before:

1. THE PURPOSE IS INSIDE THE MAC. Signing only an id with one secret means any
   token is structurally valid for any action that uses that secret. Adding a
   second purpose later would then silently accept unsubscribe tokens for it.
2. A KEY ID IS RECORDED IN THE PAYLOAD, but it is NOT a rotation mechanism
   today: `verify_token` checks against the single configured secret and does
   not consult `kid`. Rotating the signing secret therefore invalidates
   every unsubscribe link already in an inbox — and since those links have no
   expiry, that is every link ever sent. The field exists so a future keyed
   secret ring can be added without changing the token format; until that
   ships, treat a rotation as a decision to break every outstanding link.
   (An earlier version of this docstring claimed rotation already worked. It
   did not, and an operator acting on it would have silently broken the only
   way off the mailing list.)
3. VERIFICATION IS CONSTANT-TIME, via `hmac.compare_digest`. A plain `==` on
   the digest leaks position information under timing analysis, and this is a
   public endpoint.

Note there is no expiry. An unsubscribe link must work whenever someone finds
the email — expiring the only exit from a mailing list is user-hostile and, for
a bulk sender, a deliverability problem rather than a security feature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final
from uuid import UUID

TOKEN_VERSION: Final = 1
# Two purposes, not one, and the distinction is load-bearing. A host that
# mints unsubscribe links for two DIFFERENT things (here: a waiting list and
# a mailing list) must not let a token for one verify as the other — the MAC
# covers the purpose, so a shared string means no isolation at all and the
# only thing standing between them is that their subject ids happen not to
# collide. That is a property of the ids, not a guarantee of this module.
PURPOSE_UNSUBSCRIBE: Final = "unsub"
PURPOSE_LIST_UNSUBSCRIBE: Final = "list-unsub"


class InvalidTokenError(Exception):
    """The token is malformed, mis-signed, or scoped to another purpose."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()


def mint_token(*, entry_id: UUID, purpose: str, secret: str, key_id: str = "k1") -> str:
    """Return `<payload>.<mac>`, both base64url, safe for a URL path segment."""
    if not secret:
        # Never fall back to a default. A hardcoded dev secret in a signing path
        # means anyone who reads the source can unsubscribe anyone.
        raise InvalidTokenError("signing secret is not configured")
    payload = json.dumps(
        {"v": TOKEN_VERSION, "kid": key_id, "p": purpose, "id": str(entry_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64url_encode(payload)}.{_b64url_encode(_sign(payload, secret))}"


def verify_token(token: str, *, purpose: str, secret: str) -> UUID:
    """Return the entry id, or raise `InvalidTokenError`.

    Every failure raises the SAME exception with no detail about which check
    failed — a caller that distinguishes "bad signature" from "wrong purpose"
    hands an attacker an oracle.
    """
    if not secret:
        raise InvalidTokenError("signing secret is not configured")
    try:
        encoded_payload, encoded_mac = token.split(".", 1)
        payload = _b64url_decode(encoded_payload)
        mac = _b64url_decode(encoded_mac)
    except (ValueError, TypeError) as exc:
        raise InvalidTokenError("malformed token") from exc

    # compare_digest, not ==: the latter short-circuits on the first differing
    # byte, which is measurable over a public endpoint.
    if not hmac.compare_digest(mac, _sign(payload, secret)):
        raise InvalidTokenError("bad signature")

    try:
        claims = json.loads(payload)
    except ValueError as exc:
        raise InvalidTokenError("malformed payload") from exc

    if claims.get("v") != TOKEN_VERSION:
        raise InvalidTokenError("unsupported version")
    # Checked AFTER the signature, so an attacker cannot probe purposes without
    # a valid MAC — but checked at all, which is the point: same secret plus no
    # purpose claim would make every token interchangeable.
    if claims.get("p") != purpose:
        raise InvalidTokenError("wrong purpose")

    try:
        return UUID(claims["id"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("malformed subject") from exc
