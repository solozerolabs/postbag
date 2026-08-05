"""DDL text and the schema-name guard that stands in for a bind parameter."""

from __future__ import annotations

import pytest

from postbag import ddl, store


def test_statements_are_emitted_for_all_three_tables() -> None:
    sql = "\n".join(ddl.statements("app"))
    assert "app.newsletter_lists" in sql
    assert "app.newsletter_subscribers" in sql
    assert "app.newsletter_sends" in sql
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3


def test_the_send_log_carries_the_unique_idempotency_spine() -> None:
    """Reds if `UNIQUE (subscriber_id, message_key)` is dropped from the send log.

    Without it two racing ticks both insert and both send, and the engine has no
    other mechanism that would stop them — the app-level filter is an
    optimisation, not the guarantee.
    """
    sends = next(s for s in ddl.statements("app") if "newsletter_sends" in s)
    assert "UNIQUE (subscriber_id, message_key)" in sends


def test_subscribers_are_unique_per_list() -> None:
    subscribers = next(
        s for s in ddl.statements("app") if "newsletter_subscribers" in s
    )
    assert "UNIQUE (list_id, email)" in subscribers


@pytest.mark.parametrize(
    "bad",
    ["bad-schema", "x; drop", "", "1schema", "Public", "syn dai", 'a"b', "a'--"],
)
def test_invalid_schema_names_are_refused(bad: str) -> None:
    """Reds if `validate_schema_name`'s regex is loosened or its call is removed.

    The schema name is interpolated into SQL — there is no bind parameter for an
    identifier — so this regex is the injection boundary. `x; drop` is the
    concrete shape it exists to stop.
    """
    with pytest.raises(ValueError, match="invalid schema name"):
        ddl.statements(bad)


@pytest.mark.parametrize("good", ["app", "syn_data", "_private", "s1"])
def test_valid_schema_names_are_accepted(good: str) -> None:
    """Negative control: the guard rejects bad names without rejecting good ones."""
    assert ddl.statements(good)


@pytest.mark.parametrize(
    "builder",
    [
        lambda s: store.upsert_subscriber(
            s, list_id="00000000-0000-0000-0000-000000000001", email="a@b.test"
        ),
        lambda s: store.suppress_email(s, email="a@b.test", reason="bounce"),
        lambda s: store.unsubscribe_subscriber(
            s, subscriber_id="00000000-0000-0000-0000-000000000001"
        ),
        lambda s: store.record_send(
            s, subscriber_id="00000000-0000-0000-0000-000000000001", message_key="k"
        ),
        lambda s: store.select_active_subscribers(
            s, list_id="00000000-0000-0000-0000-000000000001"
        ),
        lambda s: store.select_sends(s, list_id="00000000-0000-0000-0000-000000000001"),
    ],
)
def test_every_store_builder_validates_its_schema(builder) -> None:
    """Reds if any builder is added or edited without the schema guard.

    Enumerated per-builder rather than checked once, because the leak class this
    repo keeps paying for is one call site that re-derives a normalisation and
    misses it.
    """
    with pytest.raises(ValueError, match="invalid schema name"):
        builder("x; drop")
