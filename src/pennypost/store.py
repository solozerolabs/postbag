"""Storage as SQL text plus parameters — never as a driver.

Every function here either builds a `Query` (pure, testable without a database)
or takes a caller-supplied `execute` callable. The engine therefore has no
opinion about asyncpg vs psycopg vs SQLAlchemy, no connection pool of its own,
and — the part that actually matters — no ability to open a transaction the
host did not ask for. Mirror-in sweeps and drip ticks run inside the host's
unit of work or they do not run at all.

Placeholders are `:name` style. Every driver in common use either speaks it or
has a one-line adapter to it; the alternative (`%s` positional) makes the
call sites order-dependent, which is the shape where a refactor swaps two
columns and nothing fails until production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pennypost.ddl import validate_schema_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class Query:
    """One parameterized statement. `params` is never interpolated into `sql`."""

    sql: str
    params: dict[str, Any] = field(default_factory=dict)


class Execute(Protocol):
    """The host's escape hatch: run this SQL, give back the rows.

    Rows may be anything the driver returns (mappings, tuples, ORM rows). The
    engine only ever asks "how many" or reads named keys through the small
    accessors below, so a host is free to hand back its native row type.
    """

    def __call__(
        self, sql: str, params: dict[str, Any]
    ) -> Awaitable[Sequence[Any]]: ...


def normalize_email(email: str) -> str:
    """Fold an address the ONE way this package folds addresses.

    Suppression is a consent boundary, and it already had two normalizations:
    the host's own writer lowercased and stripped, this module compared a
    raw column. A padded or mixed-case recipient from a webhook would suppress
    one store and silently miss the other — the one-normalization-many-leaks
    shape, on the seam where getting it wrong means mailing someone who asked
    you to stop.
    """
    return email.strip().lower()


def upsert_subscriber(
    schema: str,
    *,
    list_id: UUID | str,
    email: str,
    source: str | None = None,
) -> Query:
    """Enrol `email` on `list_id`, or do nothing if the row already exists.

    ON CONFLICT DO NOTHING, deliberately, and NOT `DO UPDATE`. Terminal states
    stick: once someone has unsubscribed or been suppressed, a later sweep that
    re-reads them from an upstream source must NEVER resurrect the row. An
    upsert that touched the existing row would silently re-enrol every person
    who had left, which is both the worst mail you can send and, under CAN-SPAM,
    the illegal kind.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"INSERT INTO {schema}.newsletter_subscribers (list_id, email, source) "
            "VALUES (:list_id, :email, :source) "
            "ON CONFLICT (list_id, email) DO NOTHING "
            "RETURNING id"
        ),
        params={
            "list_id": str(list_id),
            "email": normalize_email(email),
            "source": source,
        },
    )


def suppress_email(schema: str, *, email: str, reason: str) -> Query:
    """Suppress an address on EVERY list, not just the one that bounced.

    A hard bounce or a spam complaint is a fact about the mailbox, not about a
    list. Scoping suppression to one list means the next list mails the same
    dead address and the domain reputation pays for it twice.

    `suppressed_at` is only set when it is NULL, so a re-run keeps the ORIGINAL
    suppression time — that timestamp is evidence, and an idempotent sweep must
    not keep moving it forward.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"UPDATE {schema}.newsletter_subscribers "
            "SET suppressed_at = COALESCE(suppressed_at, now()), "
            "    suppressed_reason = COALESCE(suppressed_reason, :reason) "
            "WHERE email = :email AND suppressed_at IS NULL "
            "RETURNING id"
        ),
        params={"email": normalize_email(email), "reason": reason},
    )


def unsubscribe_subscriber(schema: str, *, subscriber_id: UUID | str) -> Query:
    """Mark one subscriber row as unsubscribed.

    Scoped to a single row on purpose: the unsubscribe link identifies a
    subscription, not a person, and consent is per-list. Widening this to "every
    row with this email" would silently opt someone out of a list they never
    asked to leave.

    Same COALESCE rule as suppression: a second click keeps the first timestamp.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"UPDATE {schema}.newsletter_subscribers "
            "SET unsubscribed_at = COALESCE(unsubscribed_at, now()) "
            "WHERE id = :subscriber_id "
            "RETURNING id"
        ),
        params={"subscriber_id": str(subscriber_id)},
    )


def record_send(schema: str, *, subscriber_id: UUID | str, message_key: str) -> Query:
    """Claim (subscriber, message_key). Returns a row IFF this call won the race.

    The UNIQUE index does the work; DO NOTHING turns the loser into an empty
    result instead of an exception. Callers treat "no row" as "already sent" and
    skip — never as an error to retry.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"INSERT INTO {schema}.newsletter_sends (subscriber_id, message_key) "
            "VALUES (:subscriber_id, :message_key) "
            "ON CONFLICT (subscriber_id, message_key) DO NOTHING "
            "RETURNING id"
        ),
        params={"subscriber_id": str(subscriber_id), "message_key": message_key},
    )


def release_send(schema: str, *, subscriber_id: UUID | str, message_key: str) -> Query:
    """Undo a claim the transport DEFINITELY refused, so a later tick retries.

    This exists because the package's own `SendOutcome` docstring promises it:
    "only a definite refusal (4xx, or no request made at all) releases the
    claim". Without a release primitive that sentence was fiction, and the
    single most likely first failure — an unverified sending domain answering
    403 on the very first batch — permanently recorded an entire list as
    delivered. Recovery was hand-written SQL against production.

    NEVER call this for an ambiguous outcome. A timeout or a 5xx can sit behind
    work the provider already accepted; releasing there re-mails someone whose
    copy is in their inbox, and a duplicate is the one failure this design
    refuses to risk.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"DELETE FROM {schema}.newsletter_sends "
            "WHERE subscriber_id = :subscriber_id AND message_key = :message_key "
            # RETURNING because every query in this module returns rows: hosts
            # adapt `execute` once, and a SQLAlchemy adapter calling
            # `.mappings().all()` RAISES ResourceClosedError on non-returning
            # DML. The first draft omitted it and turned the release path into
            # an exception on exactly the failure it was written for.
            "RETURNING id"
        ),
        params={"subscriber_id": str(subscriber_id), "message_key": message_key},
    )


def select_active_subscribers(schema: str, *, list_id: UUID | str) -> Query:
    """Mailable rows only: neither unsubscribed nor suppressed.

    The predicate lives in SQL as well as in `drip.due_steps` — not a duplicated
    rule but a defence in depth, because these are the only two doors to a send
    and each one is cheap to hold shut.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            "SELECT id, list_id, email, subscribed_at, unsubscribed_at, "
            "       suppressed_at, suppressed_reason, source "
            f"FROM {schema}.newsletter_subscribers "
            "WHERE list_id = :list_id "
            "  AND unsubscribed_at IS NULL "
            "  AND suppressed_at IS NULL "
            "ORDER BY subscribed_at, id"
        ),
        params={"list_id": str(list_id)},
    )


def select_suppressed_emails(schema: str, *, list_id: UUID | str) -> Query:
    """Every suppressed address on a list — the set the wire check is fed from.

    Exists as a first-class query because the send site's suppression check is
    fail-closed and has to be fed from CURRENT state, read in the same tick as
    the plan: a suppression written between planning and sending is exactly the
    one that matters. `select_active_subscribers` excludes these rows rather
    than listing them, and a host re-deriving this predicate by hand is the
    one-normalization-many-leaks shape.
    """
    validate_schema_name(schema)
    return Query(
        sql=(
            f"SELECT email FROM {schema}.newsletter_subscribers "
            "WHERE list_id = :list_id AND suppressed_at IS NOT NULL"
        ),
        params={"list_id": str(list_id)},
    )


def select_sends(
    schema: str, *, list_id: UUID | str, message_keys: Sequence[str] | None = None
) -> Query:
    """Logged sends for a list, for the drip's already-sent set and the archive.

    `message_keys` bounds the read to the messages the caller is about to
    consider. Unbounded, this grows as subscribers x messages-ever-sent and is
    re-read on EVERY tick — 10k subscribers x 52 weekly issues is ~520k rows an
    hour, forever, to answer a question about a handful of keys. The archive
    still wants everything, so the bound is the caller's to ask for.

    Correctness never depended on this read: the guarantee is the UNIQUE on
    `(subscriber_id, message_key)`, and `due_steps` treats the already-sent set
    as an optimisation. Narrowing it cannot cause a double send.
    """
    validate_schema_name(schema)
    params: dict[str, Any] = {"list_id": str(list_id)}
    predicate = ""
    if message_keys is not None:
        # ANY over an array rather than an IN with generated placeholders: one
        # bind, so the plan is stable no matter how many keys the sequence has.
        predicate = "AND s.message_key = ANY(:message_keys) "
        params["message_keys"] = list(message_keys)
    return Query(
        sql=(
            "SELECT s.id, s.subscriber_id, s.message_key, s.sent_at "
            f"FROM {schema}.newsletter_sends AS s "
            f"JOIN {schema}.newsletter_subscribers AS sub ON sub.id = s.subscriber_id "
            "WHERE sub.list_id = :list_id "
            f"{predicate}"
            "ORDER BY s.sent_at"
        ),
        params=params,
    )


async def run(execute: Execute, query: Query) -> Sequence[Any]:
    """Hand a built `Query` to the host's executor."""
    return await execute(query.sql, query.params)


async def claim_send(
    execute: Execute, schema: str, *, subscriber_id: UUID | str, message_key: str
) -> bool:
    """True if this call inserted the send row, False if it was already there.

    This is the whole idempotency API. A caller that gets False must not send.
    """
    rows = await run(
        execute,
        record_send(schema, subscriber_id=subscriber_id, message_key=message_key),
    )
    return len(rows) > 0
