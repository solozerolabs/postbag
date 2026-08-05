"""The send boundary: suppression at the wire, and the outcome mapping."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pennypost.transport_resend import (
    BATCH_MAX,
    Message,
    SendOutcome,
    chunk,
    send_batch,
)


def message(to: str) -> Message:
    return Message(
        to=to,
        subject="Issue 1",
        html_body="<p>hi</p>",
        text_body="hi",
        unsubscribe_url="https://example.test/unsubscribe/tok",
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _send(messages: list[Message], suppressed: set[str], handler: Any) -> Any:
    async with _client(handler) as client:
        return await send_batch(
            messages,
            suppressed,
            from_address="news@example.test",
            api_key="re_test",
            client=client,
            idempotency_key="idem-1",
        )


async def test_suppressed_recipient_never_reaches_the_wire() -> None:
    """Reds if `send_batch` stops calling `drop_suppressed` before building the payload.

    The assertion is on the REQUEST BODY, not on the return value: a filter that
    ran after serialisation, or only inside the caller, would still let the
    address leave the process. Deleting the drop makes `blocked@` appear here.
    """
    captured: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    outcome = await _send(
        [message("blocked@example.test"), message("ok@example.test")],
        {"blocked@example.test"},
        handler,
    )

    assert outcome is SendOutcome.SENT
    wire = json.dumps(captured)
    assert "blocked@example.test" not in wire
    # Negative control: the same run proves the wire is not simply empty.
    assert "ok@example.test" in wire
    assert [m["to"] for m in captured[0]] == [["ok@example.test"]]


async def test_suppression_match_is_case_insensitive() -> None:
    """Reds if `drop_suppressed` compares raw strings instead of case-folding.

    Mail providers do not distinguish these mailboxes; matching narrowly mails
    someone who asked never to hear from us again.
    """
    captured: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    outcome = await _send(
        [message("Blocked@Example.test")], {"blocked@example.test"}, handler
    )
    assert outcome is SendOutcome.SENT
    # No request at all: everything was dropped.
    assert captured == []


async def test_every_message_carries_both_one_click_headers() -> None:
    """Reds if either List-Unsubscribe header is dropped from `_payload`.

    List-Unsubscribe alone gives the mail client a link but not the button, and
    the Gmail/Yahoo bulk rules want the POST form.
    """
    captured: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    await _send([message("a@example.test")], set(), handler)
    headers = captured[0][0]["headers"]
    assert headers["List-Unsubscribe"] == "<https://example.test/unsubscribe/tok>"
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


async def test_idempotency_key_is_sent_as_a_header() -> None:
    """Reds if the Idempotency-Key header is dropped — a retry then double-sends."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(200, json={"data": []})

    await _send([message("a@example.test")], set(), handler)
    assert seen == ["idem-1"]


async def test_it_posts_to_the_batch_endpoint() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    await _send([message("a@example.test")], set(), handler)
    assert urls == ["https://api.resend.com/emails/batch"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, SendOutcome.SENT),
        (400, SendOutcome.REJECTED),
        (422, SendOutcome.REJECTED),
        (429, SendOutcome.REJECTED),
        (500, SendOutcome.UNKNOWN),
        (503, SendOutcome.UNKNOWN),
    ],
)
async def test_status_maps_to_outcome(status: int, expected: SendOutcome) -> None:
    """Reds if 5xx collapses into REJECTED (or 4xx into UNKNOWN).

    A 5xx can sit behind work Resend already accepted, so releasing the claim on
    it duplicates mail. A 4xx is a definite refusal and must NOT be treated as
    ambiguous, or nothing ever retries.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    assert await _send([message("a@example.test")], set(), handler) is expected


async def test_transport_error_is_unknown_not_rejected() -> None:
    """Reds if the `httpx.RequestError` branch returns REJECTED.

    A client-side timeout does not cancel Resend's server-side processing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    assert (
        await _send([message("a@example.test")], set(), handler) is SendOutcome.UNKNOWN
    )


async def test_empty_batch_makes_no_request() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"data": []})

    assert await _send([], set(), handler) is SendOutcome.SENT
    assert calls == []


async def test_oversized_batch_is_refused() -> None:
    """Reds if the BATCH_MAX guard is removed — Resend would reject the whole call."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ValueError, match="limit"):
        await _send(
            [message(f"a{i}@example.test") for i in range(BATCH_MAX + 1)],
            set(),
            handler,
        )


def test_chunk_splits_at_the_batch_limit() -> None:
    batches = chunk([message(f"a{i}@example.test") for i in range(250)])
    assert [len(b) for b in batches] == [100, 100, 50]
