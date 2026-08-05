"""The SQL builders: shape, parameterization, and the never-resurrect rule."""

from __future__ import annotations

import re
from typing import Any

import pytest

from postbag import store
from postbag.store import Query

LIST_ID = "00000000-0000-0000-0000-0000000000aa"
SUB_ID = "00000000-0000-0000-0000-0000000000bb"


def test_upsert_never_resurrects_a_terminal_row() -> None:
    """Reds if ON CONFLICT DO NOTHING becomes DO UPDATE.

    A mirror-in sweep re-reads upstream signups on every tick. If the conflict
    branch touched the existing row, every person who had unsubscribed or been
    suppressed would be re-enrolled on the next tick.
    """
    query = store.upsert_subscriber(
        "app", list_id=LIST_ID, email="a@b.test", source="mirror"
    )
    assert "ON CONFLICT (list_id, email) DO NOTHING" in query.sql
    assert "DO UPDATE" not in query.sql


def test_values_travel_as_parameters_not_as_sql() -> None:
    """Reds if any builder interpolates a value into the statement text."""
    queries = [
        store.upsert_subscriber("app", list_id=LIST_ID, email="rob'; drop--@b.test"),
        store.suppress_email("app", email="rob'; drop--@b.test", reason="complaint"),
        store.unsubscribe_subscriber("app", subscriber_id=SUB_ID),
        store.record_send("app", subscriber_id=SUB_ID, message_key="k'--"),
        store.select_active_subscribers("app", list_id=LIST_ID),
        store.select_sends("app", list_id=LIST_ID),
    ]
    for query in queries:
        for value in query.params.values():
            if isinstance(value, str) and value:
                assert value not in query.sql


def test_active_subscribers_excludes_both_terminal_states() -> None:
    """Reds if either NULL check is dropped from the mailable predicate."""
    sql = store.select_active_subscribers("app", list_id=LIST_ID).sql
    assert "unsubscribed_at IS NULL" in sql
    assert "suppressed_at IS NULL" in sql


def test_suppression_is_not_scoped_to_one_list() -> None:
    """Reds if a `list_id` predicate is added to suppression.

    A bounce is a fact about the mailbox, not about a list; scoping it means the
    next list mails the same dead address.
    """
    sql = store.suppress_email("app", email="a@b.test", reason="bounce").sql
    assert "list_id" not in sql


def test_suppression_keeps_the_first_timestamp() -> None:
    """Reds if COALESCE is removed — a re-run would keep moving the evidence forward."""
    sql = store.suppress_email("app", email="a@b.test", reason="bounce").sql
    assert "COALESCE(suppressed_at, now())" in sql


def test_unsubscribe_touches_exactly_one_subscription() -> None:
    """Reds if the WHERE clause widens to match on email.

    Consent is per-list; widening opts someone out of a list they never left.
    """
    sql = store.unsubscribe_subscriber("app", subscriber_id=SUB_ID).sql
    assert "WHERE id = :subscriber_id" in sql
    assert "email" not in sql


async def test_claim_send_reports_whether_it_won_the_insert() -> None:
    """Reds if `claim_send` returns True unconditionally.

    False means the UNIQUE index already held that (subscriber, key) — the
    caller must skip, and a True here would double-send.
    """
    rows: list[Any] = []

    async def execute(sql: str, params: dict[str, Any]) -> list[Any]:
        return rows

    assert (
        await store.claim_send(execute, "app", subscriber_id=SUB_ID, message_key="k")
        is False
    )
    rows.append({"id": "1"})
    assert (
        await store.claim_send(execute, "app", subscriber_id=SUB_ID, message_key="k")
        is True
    )


async def test_run_passes_the_built_sql_and_params_through() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def execute(sql: str, params: dict[str, Any]) -> list[Any]:
        seen.append((sql, params))
        return []

    query = store.select_sends("app", list_id=LIST_ID)
    await store.run(execute, query)
    assert seen == [(query.sql, query.params)]


def test_select_suppressed_emails_targets_only_the_suppressed_predicate() -> None:
    """The wire check's feed: list-scoped, suppressed-only.

    Mutation: drop `suppressed_at IS NOT NULL` -> this reds; drop the list_id
    bind -> this reds. A host re-deriving this SQL by hand is the
    one-normalization-many-leaks shape this query exists to prevent.
    """
    query = store.select_suppressed_emails(
        "news", list_id="11111111-1111-1111-1111-111111111111"
    )
    assert "news.newsletter_subscribers" in query.sql
    assert "suppressed_at IS NOT NULL" in query.sql
    assert query.params == {"list_id": "11111111-1111-1111-1111-111111111111"}


def test_select_suppressed_emails_validates_schema() -> None:
    with pytest.raises(ValueError, match="schema"):
        store.select_suppressed_emails("bad-schema", list_id="x")


def test_release_send_targets_exactly_one_claim() -> None:
    """The primitive the SendOutcome docstring promised and did not have.

    Mutation: drop the message_key term -> this reds, and a release would wipe
    every claim a subscriber ever had rather than the one that was refused.
    """
    query = store.release_send(
        "news", subscriber_id="11111111-1111-1111-1111-111111111111", message_key="k1"
    )
    assert "DELETE FROM news.newsletter_sends" in query.sql
    assert "subscriber_id = :subscriber_id" in query.sql
    assert "message_key = :message_key" in query.sql
    assert query.params["message_key"] == "k1"


def test_suppress_email_folds_the_address_the_one_way() -> None:
    """Two normalizations on a consent boundary is how one store misses.

    Mutation: drop normalize_email from suppress_email -> this reds.
    """
    query = store.suppress_email("news", email="  Dead@Example.COM ", reason="bounce")
    assert query.params["email"] == "dead@example.com"


def test_every_dml_query_returns_rows() -> None:
    """The whole-class guard for the blocker that shipped in round 1.

    A host adapts `Execute` ONCE, and the natural SQLAlchemy adaptation ends in
    `.mappings().all()` — which raises `ResourceClosedError` on any DML without
    RETURNING. `release_send` shipped without it and turned the 4xx release path
    into an exception on the exact failure it was written for.

    Enumerated from the module rather than from a list, so a query added
    tomorrow is covered the day it is written, not the day someone remembers.
    Mutation: drop RETURNING from any builder -> this reds naming it.
    """
    import inspect
    import uuid as _uuid

    offenders: list[str] = []
    scanned = 0
    sample = str(_uuid.uuid4())
    for name, fn in vars(store).items():
        if not callable(fn) or name.startswith("_") or not inspect.isfunction(fn):
            continue
        # The annotation is a STRING here: `from __future__ import annotations`
        # defers evaluation, so comparing against the class matched NOTHING and
        # the first version of this guard scanned zero queries while passing.
        if inspect.signature(fn).return_annotation not in (Query, "Query"):
            continue
        kwargs: dict[str, object] = {}
        for param in list(inspect.signature(fn).parameters)[1:]:
            kwargs[param] = sample if "id" in param else "x@example.com"
        query = fn("news", **kwargs)
        scanned += 1
        if (
            re.search(r"\b(INSERT|UPDATE|DELETE)\b", query.sql)
            and "RETURNING" not in query.sql
        ):
            offenders.append(name)

    assert scanned >= 5, f"the scan found only {scanned} query builders — it went stale"
    assert not offenders, (
        f"DML without RETURNING (a mappings() adapter raises): {offenders}"
    )


def test_select_sends_is_bounded_when_keys_are_given() -> None:
    """The unbounded form re-reads the whole send history on every tick.

    Mutation: drop the `message_keys` branch -> this reds.
    """
    query = store.select_sends("app", list_id=LIST_ID, message_keys=["a", "b"])
    assert "s.message_key = ANY(:message_keys)" in query.sql
    assert query.params["message_keys"] == ["a", "b"]


def test_select_sends_stays_unbounded_when_no_keys_are_given() -> None:
    """Negative control: the archive still wants everything, so the bound is
    the caller's to ask for and must not appear by default.
    """
    query = store.select_sends("app", list_id=LIST_ID)
    assert "message_key = ANY" not in query.sql
    assert "message_keys" not in query.params


def test_an_empty_key_list_is_not_the_same_as_no_list() -> None:
    """`[]` means "no message could have been sent", NOT "read everything".

    Treating an empty sequence as unbounded is the classic falsy-vs-None slip,
    and here it would silently restore the unbounded scan.
    """
    query = store.select_sends("app", list_id=LIST_ID, message_keys=[])
    assert "s.message_key = ANY(:message_keys)" in query.sql
    assert query.params["message_keys"] == []
