"""The Resend send boundary: batched, suppression-checked, tri-state.

Every message carries a working one-click unsubscribe and a postal address (the
latter via the footer the renderer appends). That is not politeness: CAN-SPAM
requires both, and the Gmail/Yahoo bulk-sender rules make `List-Unsubscribe` +
`List-Unsubscribe-Post` effectively mandatory — without them the mail lands in
spam and the domain warm-up is wasted.

SUPPRESSION IS ENFORCED HERE, AT THE WIRE, AND IT FAILS CLOSED. The database
query already excludes suppressed rows and the drip already filters them, but
this is the last door before real mail leaves, and it is the only one that
cannot be bypassed by a caller who built a message list some other way. A
suppressed recipient is dropped and logged; it is never an exception, because
one bad address must not cancel the other ninety-nine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import httpx

from postbag.store import normalize_email

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

RESEND_BATCH_API_URL: Final = "https://api.resend.com/emails/batch"
# Resend's batch endpoint takes up to 100 messages and counts as ONE request
# against the rate limit. A 250-seat wave is 3 calls, not 250.

BATCH_MAX: Final = 100

# HTTP status at or above which the outcome is UNKNOWN rather than refused.
_SERVER_ERROR = 500


class SendOutcome(StrEnum):
    """Tri-state, deliberately not a boolean.

    A client-side timeout does NOT cancel Resend's server-side processing, and a
    5xx can sit behind work that was already accepted — so neither is proof of
    non-delivery. Rolling a claim back on either re-sends to someone whose mail
    is already in their inbox. Only a definite refusal (4xx, or no request made
    at all) releases the claim; an ambiguous outcome keeps it, because a missing
    email is recoverable by hand and a duplicate one is not.
    """

    SENT = "sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Message:
    """One rendered message, ready to hand to Resend."""

    to: str
    subject: str
    html_body: str
    text_body: str
    unsubscribe_url: str


def _payload(message: Message, *, from_address: str) -> dict[str, Any]:
    return {
        "from": from_address,
        "to": [message.to],
        "subject": message.subject,
        "html": message.html_body,
        "text": message.text_body,
        "headers": {
            # Both are required for one-click. List-Unsubscribe alone gives the
            # mail client a link but not the button, and Gmail's bulk rules want
            # the POST form.
            "List-Unsubscribe": f"<{message.unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def drop_suppressed(
    messages: Iterable[Message], suppressed: Iterable[str]
) -> tuple[list[Message], list[str]]:
    """Split `messages` into (sendable, dropped-addresses).

    Comparison is case-folded on the whole address. The local part is
    technically case-sensitive per RFC 5321, but no mail provider in practice
    treats `A@x.com` and `a@x.com` as different mailboxes — and the direction of
    the error matters: matching too eagerly drops a message, matching too
    narrowly mails someone who asked never to hear from us again.
    """
    # normalize_email, NOT a second inline fold. This is the SUPPRESSION
    # boundary: if this fold and the store's ever diverge, an address the
    # database considers suppressed stops matching here and we mail someone
    # who asked us never to write again. Two identical `.strip().lower()`
    # expressions are not a shared rule, they are a coincidence with a
    # maintenance window.
    blocked = {normalize_email(address) for address in suppressed}
    sendable: list[Message] = []
    dropped: list[str] = []
    for message in messages:
        if normalize_email(message.to) in blocked:
            dropped.append(message.to)
            continue
        sendable.append(message)
    return sendable, dropped


async def send_batch(
    messages: Sequence[Message],
    suppressed: set[str],
    *,
    from_address: str,
    api_key: str,
    client: httpx.AsyncClient,
    idempotency_key: str,
) -> SendOutcome:
    """Send up to `BATCH_MAX` messages as one request, minus suppressed recipients.

    Returns the tri-state outcome rather than raising, so the caller can tell a
    refusal apart from an ambiguous result. See `SendOutcome`.

    `suppressed` is REQUIRED and positional — not a keyword with an empty
    default. A default would make "I forgot to pass it" and "nobody is
    suppressed" the same call, and the failure mode of that confusion is mail to
    people who opted out.

    The idempotency key guards a RETRY, not the record of having sent: Resend
    only dedupes within 24 hours. The `newsletter_sends` row in Postgres is the
    authoritative "this person was already mailed" marker, and it is claimed
    before the request goes out.
    """
    sendable, dropped = drop_suppressed(messages, suppressed)
    if dropped:
        logger.warning("postbag_send_suppressed_dropped count=%d", len(dropped))
    if not sendable:
        # Nothing to do is a success, not a failure — and notably NOT an
        # ambiguous outcome: no request was made, so nothing is in flight.
        return SendOutcome.SENT
    if len(sendable) > BATCH_MAX:
        raise ValueError(f"batch exceeds Resend's limit of {BATCH_MAX}")

    try:
        response = await client.post(
            RESEND_BATCH_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            json=[_payload(m, from_address=from_address) for m in sendable],
        )
    except httpx.RequestError:
        # Timeout, connection reset, DNS. The request may have been fully
        # received and processed — UNKNOWN, never a refusal.
        logger.warning("postbag_send_ambiguous reason=transport")
        return SendOutcome.UNKNOWN

    if response.status_code >= _SERVER_ERROR:
        logger.warning(
            "postbag_send_ambiguous reason=server_error status=%d",
            response.status_code,
        )
        return SendOutcome.UNKNOWN
    if response.is_error:
        # A 4xx is Resend telling us it did NOT accept this. Safe to release.
        logger.error("postbag_send_rejected status=%d", response.status_code)
        return SendOutcome.REJECTED
    return SendOutcome.SENT


def chunk[T](items: Sequence[T], size: int = BATCH_MAX) -> list[list[T]]:
    """Split any sequence into batches — generic on purpose.

    It used to be `Sequence[Message]`, which forced every caller to chunk the
    messages and then recover the parallel records they belong to by index
    arithmetic. That is the one place in this design where a slip produces
    DUPLICATE mail: claim one set of people, send to another. Chunking the
    caller's own record type and reading `.message` off it removes the pairing
    entirely, and the body never needed the narrower type — it is slicing.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]
