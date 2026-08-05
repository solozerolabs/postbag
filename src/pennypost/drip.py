"""Drip sequences: markdown files on disk, and a pure function that says who is due.

Two rules carry the whole design.

APPROVAL IS A FILE EDIT. A step auto-sends only when its front-matter says
`approved: true`. There is no admin toggle and no database column, because the
seam that actually prevents a bad send is a human reading the copy and
committing the change — reviewable, revertible, and attributable to a person.
Anything less than `approved: true` is a draft, including a missing key.

MESSAGE KEYS ARE IMMUTABLE, AND THE FILENAME PROVES IT. `newsletter_sends` is
keyed on `(subscriber_id, message_key)`, so renaming a key makes it a new
message and re-mails the entire list. Requiring the front-matter `key` to equal
the filename stem (after the `NN-` ordering prefix) turns that rule into
something a loader can refuse, instead of something a comment asks for.

`due_steps` reads no clock and touches no disk: `now` is a parameter. A
scheduling function that calls `datetime.now()` internally can only be tested at
whatever time the test runs, which is how "it works except near midnight" ships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# `NN-` ordering prefix, then the key. The prefix orders the directory listing
# for humans; the key is what the database remembers.
_FILENAME_RE: Final = re.compile(r"^(\d+)-(.+)$")

_FRONT_MATTER_FENCE: Final = "---"


class DripError(Exception):
    """A sequence directory that cannot be trusted to send from."""


class SendCapExceeded(Exception):
    """The tick wanted to send more mail than a healthy tick ever should.

    Raised instead of truncating. A drip bug's worst case is repeat-mailing real
    people, and there is no safe partial version of that: sending the first 200
    of a runaway batch is still 200 wrong emails. Refusing the whole pass
    converts the bug into one alert and zero delivered mistakes, and the state
    it refused is still on disk and in Postgres when a human looks.
    """


@dataclass(frozen=True, slots=True)
class DripStep:
    """One approved-or-not step of a sequence."""

    key: str
    day: int
    subject: str
    approved: bool
    body: str
    order: int
    path: Path | None = None
    # A CALENDAR send — one issue, to the people already on the list when it was
    # written — is structurally this step with `day: 0` and a cutoff. Without the
    # cutoff it is a trap: `day: 0` also fires for everyone who joins LATER, so a
    # new subscriber's welcome would be weeks of stale updates, oldest first. A
    # drip step leaves this None and is unaffected.
    #
    # An INSTANT, not a date. A bare `2026-08-04` has no unambiguous meaning
    # across zones — a signup at 18:00 Pacific on the 4th is the 5th in UTC — so
    # a date silently made the audience depend on the reader's timezone. It also
    # could not express WHEN the issue was written, only which day, which is the
    # quantity the audience actually depends on.
    cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class Subscriber:
    """The subset of a subscriber row the drip actually reads."""

    id: str
    email: str
    subscribed_at: datetime
    unsubscribed_at: datetime | None = None
    suppressed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DueSend:
    """One (subscriber, step) pair that is due right now."""

    subscriber: Subscriber
    step: DripStep

    @property
    def message_key(self) -> str:
        return self.step.key


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split `---` front matter from the body. Returns `({}, text)` when absent.

    Hand-parsed rather than handed to a YAML library: a YAML parser in this
    position is a dependency, an arbitrary-type surface (`day: 2026-01-01`
    becoming a date, `approved: yes` becoming a bool by a rule nobody
    remembers), and a load-time code-execution question. Four keys of
    `key: value` do not need any of that.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        return {}, text
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONT_MATTER_FENCE:
            body = "\n".join(lines[index + 1 :])
            return meta, body.lstrip("\n")
        if not line.strip():
            continue
        field, separator, value = line.partition(":")
        if not separator:
            raise DripError(f"front matter line is not `key: value`: {line!r}")
        meta[field.strip()] = value.strip()
    raise DripError("front matter is not closed by `---`")


def _parse_bool(value: str, *, where: str) -> bool:
    """Only the two literal spellings. Anything else is an error, never a False.

    Silently treating `approved: maybe` as False would be the safe direction,
    but it hides a typo in the one field that gates sending mail — and the same
    typo in the other direction (`approved: ture`) would then read as "still a
    draft" forever, with the author believing it shipped.
    """
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    raise DripError(f"{where}: `approved` must be `true` or `false`, got {value!r}")


def load_step(path: Path) -> DripStep:
    """Parse one `NN-key-name.md` file, refusing anything ambiguous."""
    match = _FILENAME_RE.match(path.stem)
    if not match:
        raise DripError(f"{path.name}: filename must be `NN-key-name.md`")
    order_text, stem_key = match.groups()

    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    missing = [k for k in ("key", "day", "subject", "approved") if k not in meta]
    if missing:
        raise DripError(f"{path.name}: missing front-matter {', '.join(missing)}")

    if meta["key"] != stem_key:
        # The immutability rule, made checkable. If these two can drift, the
        # filename stops being evidence of what the database remembers.
        raise DripError(
            f"{path.name}: front-matter key {meta['key']!r} must equal the "
            f"filename stem after the `NN-` prefix ({stem_key!r}) — message keys "
            "are immutable, and a renamed key re-mails the whole list"
        )

    try:
        day = int(meta["day"])
    except ValueError as exc:
        raise DripError(
            f"{path.name}: `day` must be an integer, got {meta['day']!r}"
        ) from exc
    if day < 0:
        raise DripError(f"{path.name}: `day` must not be negative, got {day}")
    if not meta["subject"]:
        raise DripError(f"{path.name}: `subject` must not be empty")

    return DripStep(
        key=meta["key"],
        day=day,
        subject=meta["subject"],
        approved=_parse_bool(meta["approved"], where=path.name),
        body=body,
        order=int(order_text),
        path=path,
        cutoff=_parse_cutoff(meta.get("cutoff"), where=path.name),
    )


def _parse_cutoff(value: str | None, *, where: str) -> datetime | None:
    """Optional ISO-8601 instant WITH an offset. Absent means "no cutoff".

    Never a silent None on a bad value: this field NARROWS an audience, so a
    typo that reads as "no cutoff" mails the whole list instead of the slice the
    author meant — the failure direction that costs subscribers.

    The offset is REQUIRED rather than defaulted to UTC. A naive timestamp here
    would be compared against tz-aware `subscribed_at` and raise at send time,
    and guessing UTC for it would silently shift the audience by up to a day.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DripError(
            f"{where}: `cutoff` must be an ISO-8601 instant with an offset "
            f"(e.g. 2026-08-04T17:30:00+00:00), got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise DripError(
            f"{where}: `cutoff` must carry a UTC offset — {value!r} is naive, "
            "and its meaning would depend on the reader's timezone"
        )
    return parsed


def load_sequence(directory: Path, *, require_cutoff: bool = False) -> list[DripStep]:
    """Load every `*.md` in `directory`, ordered by day then filename prefix.

    `require_cutoff` is for a CALENDAR directory (an outbox), where a step
    without one is the worst shape this package can produce: `day: 0` and
    `approved: true` with no cutoff mails that issue to every subscriber who
    ever joins, forever. It cannot be inferred from `day == 0` alone, because a
    day-0 DRIP step is a legitimate send-on-signup welcome — the difference is
    the role the directory plays, which only the caller knows.

    Enforced at LOAD so the validator and the runtime cannot disagree: the same
    call that the gate makes is the one the tick makes.
    """
    if not directory.is_dir():
        raise DripError(f"not a directory: {directory}")
    steps = [load_step(path) for path in sorted(directory.glob("*.md"))]
    if require_cutoff:
        uncapped = [s.path.name if s.path else s.key for s in steps if s.cutoff is None]
        if uncapped:
            raise DripError(
                "calendar steps must declare a `cutoff`, or they mail every "
                f"future subscriber: {', '.join(sorted(uncapped))}"
            )
    duplicates = {
        s.key for s in steps if sum(1 for other in steps if other.key == s.key) > 1
    }
    if duplicates:
        raise DripError(f"duplicate message keys: {', '.join(sorted(duplicates))}")
    # Key breaks ties: calendar issues all carry `day: 0` and share an ordering
    # prefix, so day+order alone leaves their order down to dict iteration.
    return sorted(steps, key=lambda s: (s.day, s.order, s.key))


def normalize_sends(sends: Iterable[Any]) -> set[tuple[str, str]]:
    """Coerce logged sends to `{(subscriber_id, message_key)}`.

    Accepts pairs or mappings, so a host can pass driver rows straight through
    without building an intermediate type it does not otherwise need.
    """
    pairs: set[tuple[str, str]] = set()
    for send in sends:
        if isinstance(send, tuple):
            subscriber_id, message_key = send
        else:
            subscriber_id = send["subscriber_id"]
            message_key = send["message_key"]
        pairs.add((str(subscriber_id), str(message_key)))
    return pairs


def due_steps(
    subscribers: Sequence[Subscriber],
    sends: Iterable[Any],
    sequence: Sequence[DripStep],
    now: datetime,
) -> list[DueSend]:
    """Every (subscriber, step) that should be sent at `now`. Pure and ordered.

    A step is due when ALL of these hold:
      * the subscriber has neither unsubscribed nor been suppressed,
      * `subscribed_at + day` days have elapsed at `now`,
      * the step is `approved: true`,
      * the step has no `cutoff`, or the subscriber joined on or before it,
      * `(subscriber_id, key)` has not already been logged.

    The last one is an optimisation, not the guarantee. The guarantee is the
    UNIQUE index in `newsletter_sends`; this filter only keeps the tick from
    doing obvious work it will lose at the database anyway.
    """
    already = normalize_sends(sends)
    due: list[DueSend] = []
    for subscriber in subscribers:
        if (
            subscriber.unsubscribed_at is not None
            or subscriber.suppressed_at is not None
        ):
            continue
        for step in sequence:
            if not step.approved:
                continue
            if (subscriber.id, step.key) in already:
                continue
            if subscriber.subscribed_at + timedelta(days=step.day) > now:
                continue
            if step.cutoff is not None and subscriber.subscribed_at > step.cutoff:
                continue
            due.append(DueSend(subscriber=subscriber, step=step))
    return due


def send_cap(subscriber_count: int, approved_step_count: int) -> int:
    """The most messages one tick could LEGITIMATELY send.

    Derived, not chosen. `due_steps` emits at most one message per
    (subscriber, approved step), so subscribers x approved-steps IS the honest
    ceiling — exceeding it means something upstream is duplicating work, which
    is the only thing a backstop should fire on.

    Two earlier shapes were both wrong in instructive ways. `max(2N, 200)` gave
    a one-subscriber list a cap of 200: not a backstop, a rounding error. Plain
    `2N` fixed that and introduced a permanent stall — a third approved step
    plus any send outage puts three messages on one subscriber's queue, over
    the cap, forever, with no partial drain. A derived ceiling cannot stall on
    legitimate work by construction, because legitimate work cannot exceed it.
    """
    return subscriber_count * approved_step_count


def plan_tick(
    subscribers: Sequence[Subscriber],
    sends: Iterable[Any],
    sequence: Sequence[DripStep],
    now: datetime,
) -> list[DueSend]:
    """`due_steps` plus the per-tick cap. This is what a scheduler calls.

    `due_steps` stays uncapped so that tests, dry runs, and archive tooling can
    look at the true set; the cap lives on the path that actually sends.
    """
    due = due_steps(subscribers, sends, sequence, now)
    cap = send_cap(len(subscribers), sum(1 for step in sequence if step.approved))
    if len(due) > cap:
        raise SendCapExceeded(
            f"tick would send {len(due)} messages to {len(subscribers)} subscribers, "
            f"over the cap of {cap}; refusing the whole pass"
        )
    return due
