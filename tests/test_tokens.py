"""Token round-trip and the three ways it must refuse."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from postbag.tokens import (
    PURPOSE_LIST_UNSUBSCRIBE,
    PURPOSE_UNSUBSCRIBE,
    InvalidTokenError,
    _b64url_encode,
    _sign,
    mint_token,
    verify_token,
)

SECRET = "s3cr3t-for-tests-only"


def test_round_trip_returns_the_same_id() -> None:
    entry_id = uuid4()
    token = mint_token(entry_id=entry_id, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)
    assert verify_token(token, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET) == entry_id


def test_tampered_payload_is_refused() -> None:
    """Reds if `verify_token` drops the `compare_digest` check on the MAC.

    Flipping one character of the payload leaves a well-formed token whose MAC
    no longer matches; without the signature check it would verify and hand back
    somebody else's id.
    """
    token = mint_token(entry_id=uuid4(), purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)
    payload, mac = token.split(".", 1)
    tampered = f"{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{mac}"
    with pytest.raises(InvalidTokenError):
        verify_token(tampered, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)


def test_a_valid_token_for_another_purpose_is_refused() -> None:
    """Reds if the `claims["p"] != purpose` check is deleted.

    The MAC here is genuine — same secret, same id — so only the purpose claim
    stands between an unsubscribe token and some future action that reuses the
    signing secret.
    """
    token = mint_token(entry_id=uuid4(), purpose="some-other-purpose", secret=SECRET)
    with pytest.raises(InvalidTokenError):
        verify_token(token, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)


def test_wrong_secret_is_refused() -> None:
    """Reds if `_sign` ignores the secret, or verification uses a fixed key."""
    token = mint_token(entry_id=uuid4(), purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)
    with pytest.raises(InvalidTokenError):
        verify_token(token, purpose=PURPOSE_UNSUBSCRIBE, secret="a-different-secret")


def test_empty_secret_refuses_to_mint() -> None:
    """Reds if `mint_token` falls back to a default secret when none is configured.

    A hardcoded dev secret in a signing path means anyone who reads the source
    can unsubscribe anyone.
    """
    with pytest.raises(InvalidTokenError):
        mint_token(entry_id=uuid4(), purpose=PURPOSE_UNSUBSCRIBE, secret="")


def test_empty_secret_refuses_to_verify() -> None:
    """Reds if `verify_token` skips its empty-secret guard.

    Without it, an unconfigured deployment would verify tokens against `b""` —
    which an attacker can also sign with.
    """
    token = mint_token(entry_id=uuid4(), purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)
    with pytest.raises(InvalidTokenError):
        verify_token(token, purpose=PURPOSE_UNSUBSCRIBE, secret="")


def test_malformed_token_is_refused_not_crashed() -> None:
    for bad in ("", "no-dot", "!!!.???", "."):
        with pytest.raises(InvalidTokenError):
            verify_token(bad, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)


def test_correctly_signed_non_object_payload_is_refused_not_crashed() -> None:
    """Reds if `verify_token` calls `claims.get` before checking its shape.

    A JSON array signs and decodes fine, but has no `.get` method: without the
    `isinstance(claims, dict)` guard this raises `AttributeError` instead of
    the uniform `InvalidTokenError`, leaking an internal detail across the
    public boundary.
    """
    payload = json.dumps(["not", "an", "object"], separators=(",", ":")).encode()
    token = f"{_b64url_encode(payload)}.{_b64url_encode(_sign(payload, SECRET))}"

    with pytest.raises(InvalidTokenError):
        verify_token(token, purpose=PURPOSE_UNSUBSCRIBE, secret=SECRET)


def test_a_token_for_one_purpose_never_verifies_as_the_other() -> None:
    """The two purposes must be genuinely non-interchangeable.

    This is the enforcement point for a host that mints unsubscribe links for
        TWO different things — say a queue for early access and a mailing list —
        where leaving one must never cost someone their place in the other. If both
        paths share a purpose string, the MAC covers the same claim for both and the
        only thing keeping them apart is that their subject ids happen not to
        collide: a property of the ids, not a guarantee of this module.

        Mutation: point either call site back at the other constant -> this reds.
    """
    entry = uuid4()
    secret = "s3cret-value"

    list_token = mint_token(
        entry_id=entry, purpose=PURPOSE_LIST_UNSUBSCRIBE, secret=secret
    )
    other_token = mint_token(entry_id=entry, purpose=PURPOSE_UNSUBSCRIBE, secret=secret)

    # Each verifies under its own purpose...
    assert (
        verify_token(list_token, purpose=PURPOSE_LIST_UNSUBSCRIBE, secret=secret)
        == entry
    )
    assert (
        verify_token(other_token, purpose=PURPOSE_UNSUBSCRIBE, secret=secret) == entry
    )

    # ...and neither under the other's, even with the SAME subject and secret.
    with pytest.raises(InvalidTokenError):
        verify_token(list_token, purpose=PURPOSE_UNSUBSCRIBE, secret=secret)
    with pytest.raises(InvalidTokenError):
        verify_token(other_token, purpose=PURPOSE_LIST_UNSUBSCRIBE, secret=secret)
