"""Drip loading (the immutability rule) and due-step selection (the cap)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pennypost.drip as drip_module
from pennypost.drip import (
    DripError,
    DripStep,
    SendCapExceeded,
    Subscriber,
    due_steps,
    load_sequence,
    load_step,
    plan_tick,
    send_cap,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
NOW_LATER = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def write_step(
    directory: Path,
    filename: str,
    *,
    key: str,
    day: int = 2,
    subject: str = "Hello",
    approved: str = "true",
    body: str = "Body text.",
) -> Path:
    path = directory / filename
    path.write_text(
        f"---\nkey: {key}\nday: {day}\nsubject: {subject}\n"
        f"approved: {approved}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def step(key: str, day: int, *, approved: bool = True) -> DripStep:
    return DripStep(key=key, day=day, subject=key, approved=approved, body="b", order=1)


def subscriber(sub_id: str, days_ago: int, **kwargs: object) -> Subscriber:
    return Subscriber(
        id=sub_id,
        email=f"{sub_id}@example.test",
        subscribed_at=NOW - timedelta(days=days_ago),
        **kwargs,  # type: ignore[arg-type]
    )


def test_load_step_reads_all_four_front_matter_fields(tmp_path: Path) -> None:
    path = write_step(
        tmp_path, "01-what-it-is.md", key="what-it-is", day=2, subject="What it is"
    )
    loaded = load_step(path)
    assert (loaded.key, loaded.day, loaded.subject, loaded.approved) == (
        "what-it-is",
        2,
        "What it is",
        True,
    )
    assert loaded.body.strip() == "Body text."


def test_key_must_equal_the_filename_stem(tmp_path: Path) -> None:
    """Reds if the filename↔key equality check is removed from `load_step`.

    `newsletter_sends` is keyed on `(subscriber_id, message_key)`, so a key that
    can drift from its filename is a key that can be renamed by accident — and a
    renamed key re-mails the entire list.
    """
    path = write_step(tmp_path, "01-what-it-is.md", key="something-else")
    with pytest.raises(DripError, match="immutable"):
        load_step(path)


def test_filename_without_an_ordering_prefix_is_refused(tmp_path: Path) -> None:
    path = write_step(tmp_path, "what-it-is.md", key="what-it-is")
    with pytest.raises(DripError, match="NN-key-name"):
        load_step(path)


def test_missing_front_matter_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "01-a.md"
    path.write_text("---\nkey: a\nday: 1\n---\nbody\n", encoding="utf-8")
    with pytest.raises(DripError, match="missing front-matter"):
        load_step(path)


def test_unclosed_front_matter_is_refused(tmp_path: Path) -> None:
    """Reds if `parse_front_matter` returns what it has instead of raising at EOF.

    Every line here is well-formed `key: value`, so the ONLY thing that catches
    the missing closing fence is the fall-through raise. Silently accepting the
    file would treat the body as front matter and send an empty email.
    """
    path = tmp_path / "01-a.md"
    path.write_text(
        "---\nkey: a\nday: 1\nsubject: s\napproved: true\nnote: body text\n",
        encoding="utf-8",
    )
    with pytest.raises(DripError, match="not closed"):
        load_step(path)


def test_a_front_matter_line_that_is_not_key_value_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "01-a.md"
    path.write_text("---\nkey: a\njust a sentence\n---\nbody\n", encoding="utf-8")
    with pytest.raises(DripError, match="not `key: value`"):
        load_step(path)


def test_a_non_boolean_approved_is_an_error_not_a_false(tmp_path: Path) -> None:
    """Reds if `_parse_bool` falls back to False on an unrecognised spelling.

    Silently reading `approved: ture` as "still a draft" leaves the author
    believing the step shipped, forever.
    """
    path = write_step(tmp_path, "01-a.md", key="a", approved="ture")
    with pytest.raises(DripError, match="must be `true` or `false`"):
        load_step(path)


def test_duplicate_keys_in_a_sequence_are_refused(tmp_path: Path) -> None:
    write_step(tmp_path, "01-a.md", key="a")
    (tmp_path / "02-a.md").write_text(
        "---\nkey: a\nday: 3\nsubject: s\napproved: true\n---\nb\n", encoding="utf-8"
    )
    with pytest.raises(DripError, match="duplicate message keys"):
        load_sequence(tmp_path)


def test_sequence_is_ordered_by_day(tmp_path: Path) -> None:
    write_step(tmp_path, "01-a.md", key="a", day=10)
    write_step(tmp_path, "02-b.md", key="b", day=2)
    assert [s.key for s in load_sequence(tmp_path)] == ["b", "a"]


def test_a_step_whose_day_has_elapsed_is_due() -> None:
    due = due_steps([subscriber("s1", days_ago=3)], [], [step("a", 2)], NOW)
    assert [(d.subscriber.id, d.message_key) for d in due] == [("s1", "a")]


def test_a_step_whose_day_has_not_elapsed_is_not_due() -> None:
    """Reds if the offset comparison flips to `<` or drops the `subscribed_at` base."""
    assert due_steps([subscriber("s1", days_ago=1)], [], [step("a", 2)], NOW) == []


def test_an_unapproved_step_never_fires() -> None:
    """Reds if the `approved` gate is removed from `due_steps`.

    The file edit and its commit ARE the approval seam; without this check the
    engine auto-sends drafts.
    """
    assert (
        due_steps(
            [subscriber("s1", days_ago=30)], [], [step("a", 2, approved=False)], NOW
        )
        == []
    )


def test_an_already_sent_step_is_skipped() -> None:
    """Reds if the already-sent filter is dropped — every tick would re-send."""
    sends = [{"subscriber_id": "s1", "message_key": "a"}]
    assert due_steps([subscriber("s1", days_ago=9)], sends, [step("a", 2)], NOW) == []


def test_already_sent_accepts_plain_pairs_too() -> None:
    assert (
        due_steps([subscriber("s1", days_ago=9)], [("s1", "a")], [step("a", 2)], NOW)
        == []
    )


def test_an_unsubscribed_subscriber_is_skipped() -> None:
    """Reds if the terminal-state guard is removed from `due_steps`.

    Mailing someone after they unsubscribed is the failure the whole engine
    exists to make impossible.
    """
    who = subscriber("s1", days_ago=9, unsubscribed_at=NOW)
    assert due_steps([who], [], [step("a", 2)], NOW) == []


def test_a_suppressed_subscriber_is_skipped() -> None:
    """Reds if the suppression half of the terminal-state guard is dropped."""
    who = subscriber("s1", days_ago=9, suppressed_at=NOW)
    assert due_steps([who], [], [step("a", 2)], NOW) == []


def test_due_steps_reads_no_clock() -> None:
    """Reds if `due_steps` calls `datetime.now()` instead of using its `now` argument.

    Moving `now` back before the offset must change the answer; a function that
    read the wall clock would return the same list for both.
    """
    people = [subscriber("s1", days_ago=3)]
    assert due_steps(people, [], [step("a", 2)], NOW) != []
    assert due_steps(people, [], [step("a", 2)], NOW - timedelta(days=2)) == []


def test_the_send_cap_is_the_legitimate_maximum() -> None:
    """subscribers x approved steps — derived, not chosen.

    Two earlier shapes failed in opposite directions and both are pinned here:
    a floor (`max(2N, 200)`) let one subscriber take 200 messages, and a plain
    `2N` stalled forever once a third approved step backed up. A derived
    ceiling cannot do either.
    Mutation: reintroduce any floor or any constant multiple -> these fail.
    """
    assert send_cap(0, 2) == 0
    assert send_cap(1, 2) == 2
    assert send_cap(50, 1) == 50
    assert send_cap(3, 201) == 603


def test_plan_tick_refuses_a_pass_that_duplicates_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop fires on the only thing that should trip it: duplication.

    With a derived ceiling, legitimate work can never exceed the cap — so the
    cap is an assertion that `due_steps` has not started emitting the same
    (subscriber, step) twice. That is simulated here rather than hoped for.
    Mutation: truncate instead of raising, or drop the cap -> this reds.
    """
    people = [subscriber(f"s{i}", days_ago=400) for i in range(3)]
    sequence = [step("k0", 1)]
    real = drip_module.due_steps
    monkeypatch.setattr(
        drip_module, "due_steps", lambda *a, **k: list(real(*a, **k)) * 2
    )
    with pytest.raises(SendCapExceeded, match="refusing the whole pass"):
        plan_tick(people, [], sequence, NOW)


def test_plan_tick_passes_a_normal_pass_through() -> None:
    """Negative control: the cap does not simply refuse everything."""
    people = [subscriber(f"s{i}", days_ago=5) for i in range(50)]
    assert len(plan_tick(people, [], [step("a", 2)], NOW)) == 50


# --- calendar issues: `day: 0` plus a cutoff --------------------------------
#
# The whole reason `cutoff` exists. A weekly product update is `day: 0`, which
# on its own fires for everyone who ever joins — so someone who signs up in
# December would receive August's issue as their welcome. These pin the
# narrowing, in both directions.


def issue(key: str, cutoff: datetime | None) -> DripStep:
    return DripStep(
        key=key, day=0, subject=key, approved=True, body="b", order=0, cutoff=cutoff
    )


def test_a_subscriber_who_joined_after_the_cutoff_never_gets_the_issue() -> None:
    """Mutation: drop the cutoff branch in `due_steps` -> this reds."""
    joined_later = subscriber("late", days_ago=0)  # joined NOW, cutoff is earlier
    earlier = issue("weekly", NOW - timedelta(hours=1))
    due = due_steps([joined_later], [], [earlier], NOW)
    assert due == []


def test_a_subscriber_who_joined_exactly_at_the_cutoff_does_get_it() -> None:
    """The boundary is inclusive: `>`, not `>=`.

    Mutation: make the comparison `>=` -> this reds. Without it the exclusion
    test above passes against a cutoff that silently drops whoever subscribed in
    the same instant the draft was written.
    """
    joined_at_cutoff = subscriber("boundary", days_ago=0)
    due = due_steps([joined_at_cutoff], [], [issue("weekly", NOW)], NOW)
    assert [d.message_key for d in due] == ["weekly"]


def test_an_evening_pacific_signup_is_not_dropped_by_a_utc_day_boundary() -> None:
    """The reason `cutoff` is an instant and not a date.

    18:00 Pacific on the 4th is 01:00 UTC on the 5th. Under the old
    `subscribed_at.date() > cutoff_date` predicate that subscriber was excluded
    from "the August 4 issue" purely because of the zone the comparison happened
    to use. With an instant there is no day boundary to land on the wrong side
    of: a draft written after they joined includes them.
    """
    evening_pacific = Subscriber(
        id="pacific",
        email="p@example.test",
        subscribed_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
    )
    written_later = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    due = due_steps([evening_pacific], [], [issue("weekly", written_later)], NOW_LATER)
    assert [d.message_key for d in due] == ["weekly"]


def test_a_step_without_a_cutoff_is_unaffected() -> None:
    """The drip path must not change shape because calendar sends exist."""
    due = due_steps([subscriber("s", days_ago=5)], [], [step("welcome", day=2)], NOW)
    assert [d.message_key for d in due] == ["welcome"]


def test_cutoff_is_parsed_from_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "00-weekly-2026-08-04.md"
    path.write_text(
        "---\nkey: weekly-2026-08-04\nday: 0\ncutoff: 2026-08-04T17:30:00+00:00\n"
        "subject: This week\napproved: false\n---\nBody.\n",
        encoding="utf-8",
    )
    assert load_step(path).cutoff == datetime(2026, 8, 4, 17, 30, tzinfo=UTC)


def test_a_naive_cutoff_is_refused(tmp_path: Path) -> None:
    """A bare date has no unambiguous audience, and would raise at send time
    when compared against tz-aware `subscribed_at`. Refuse it at load.
    """
    path = tmp_path / "00-weekly.md"
    path.write_text(
        "---\nkey: weekly\nday: 0\ncutoff: 2026-08-04\n"
        "subject: s\napproved: true\n---\nBody.\n",
        encoding="utf-8",
    )
    with pytest.raises(DripError, match="naive"):
        load_step(path)


def test_a_malformed_cutoff_is_an_error_not_a_none(tmp_path: Path) -> None:
    """A typo must not read as "no cutoff" — that widens the audience to all."""
    path = tmp_path / "00-weekly.md"
    path.write_text(
        "---\nkey: weekly\nday: 0\ncutoff: last-tuesday\n"
        "subject: s\napproved: true\n---\nBody.\n",
        encoding="utf-8",
    )
    with pytest.raises(DripError, match="cutoff"):
        load_step(path)


def test_an_absent_cutoff_stays_none(tmp_path: Path) -> None:
    assert load_step(write_step(tmp_path, "01-a.md", key="a")).cutoff is None


def test_a_calendar_directory_refuses_a_step_with_no_cutoff(tmp_path: Path) -> None:
    """The worst shape this package can produce, refused at load.

    `day: 0` + `approved: true` + no cutoff mails that issue to every subscriber
    who ever joins, forever. Two independent reviewers raised it; nothing
    enforced it, because the only thing setting a cutoff was the writer's own
    good behaviour.
    """
    write_step(tmp_path, "00-weekly.md", key="weekly", day=0)
    with pytest.raises(DripError, match="must declare a `cutoff`"):
        load_sequence(tmp_path, require_cutoff=True)


def test_the_same_directory_loads_fine_when_the_cutoff_is_present() -> None:
    """Negative control: the rule refuses the missing field, not the directory."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "00-weekly.md").write_text(
            "---\nkey: weekly\nday: 0\ncutoff: 2026-08-04T17:30:00+00:00\n"
            "subject: s\napproved: true\n---\nBody.\n",
            encoding="utf-8",
        )
        loaded = load_sequence(directory, require_cutoff=True)
        assert [s.key for s in loaded] == ["weekly"]


def test_a_drip_directory_still_allows_a_missing_cutoff(tmp_path: Path) -> None:
    """The rule is the CALLER's, not the loader's default.

    A day-0 drip step is a legitimate send-on-signup welcome, so `day == 0`
    cannot itself imply the requirement — only the role the directory plays can.
    """
    write_step(tmp_path, "01-welcome.md", key="welcome", day=0)
    assert [s.key for s in load_sequence(tmp_path)] == ["welcome"]
