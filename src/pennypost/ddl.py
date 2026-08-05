"""The engine's tables, as plain CREATE TABLE text.

The engine never runs these itself. It hands the host a list of statements and
the host decides where they go — a migration tool, a bootstrap script, a test
fixture. That split matters for one reason worth stating plainly: a MIGRATION
must freeze its DDL. If a migration body called `statements()` it would track a
moving pin, and a database rebuilt from scratch six months from now would get
today's schema under an old revision id. Copy the text into the migration; come
back here for the next revision.

The idempotency spine is a database constraint, not application logic. See
`newsletter_sends` below.
"""

from __future__ import annotations

import re
from typing import Final

# Postgres unquoted-identifier shape. The schema name is INTERPOLATED into SQL
# (there is no bind parameter for an identifier), so this pattern is the only
# thing standing between a caller-supplied string and injection. Reject rather
# than quote: a schema whose name needs quoting is a mistake upstream.
_SCHEMA_RE: Final = re.compile(r"^[a-z_][a-z0-9_]*$")


def validate_schema_name(schema: str) -> str:
    """Return `schema` unchanged, or raise `ValueError`.

    Shared by `ddl` and `store` on purpose: two copies of an injection guard is
    the shape where one of them quietly stops matching the other.
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    return schema


def statements(schema: str) -> list[str]:
    """Return the CREATE TABLE statements for `schema`, in dependency order."""
    validate_schema_name(schema)
    return [
        f"""
CREATE TABLE IF NOT EXISTS {schema}.newsletter_lists (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
""".strip(),
        f"""
CREATE TABLE IF NOT EXISTS {schema}.newsletter_subscribers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Plain REFERENCES, deliberately no ON DELETE action: rows in this model
    -- are never deleted (leaving is a timestamp), so the default NO ACTION
    -- refuses the delete that would orphan a send log — a CASCADE would
    -- silently eat the idempotency history instead.
    list_id uuid NOT NULL REFERENCES {schema}.newsletter_lists (id),
    email text NOT NULL,
    subscribed_at timestamptz NOT NULL DEFAULT now(),
    -- Terminal states. Both are nullable timestamps rather than a status enum so
    -- that "when did they leave" survives, and so an upsert can never move a row
    -- backwards out of one (see store.upsert_subscriber).
    unsubscribed_at timestamptz,
    suppressed_at timestamptz,
    suppressed_reason text,
    source text,
    UNIQUE (list_id, email)
)
""".strip(),
        f"""
CREATE TABLE IF NOT EXISTS {schema}.newsletter_sends (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subscriber_id uuid NOT NULL REFERENCES {schema}.newsletter_subscribers (id),
    message_key text NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    -- THE IDEMPOTENCY SPINE. Not app logic, not a cache, not an advisory lock:
    -- the database refuses the second send. Two ticks racing, a retried job, a
    -- redelivered queue message — all of them collide here and lose.
    -- The corollary is that `message_key` is IMMUTABLE once anything has logged
    -- it. Renaming a key makes it a new message and re-mails the whole list, so
    -- drip filenames of sent steps never change (drip.py enforces the
    -- filename↔key equality that makes that rule checkable).
    UNIQUE (subscriber_id, message_key)
)
""".strip(),
        # Suppression is BY EMAIL across every list (a bounce is a fact about the
        # mailbox), so the predicate is email-only and the UNIQUE above leads
        # with list_id — it cannot serve this lookup. Without the index it is a
        # sequential scan per recipient on every bounce/complaint webhook.
        (
            "CREATE INDEX IF NOT EXISTS ix_newsletter_subscribers_email "
            f"ON {schema}.newsletter_subscribers (email)"
        ),
    ]
