"""Static HTML archive, generated from the send log.

The send log is the source of truth about what went out, so the archive is
derived from it rather than from the messages directory: a draft that was never
sent has no page, and a page that exists is evidence that mail was delivered.

The output is plain, self-contained HTML with no assets, no scripts, and no
external requests. Partly that is so it can be dropped behind any static host or
opened from a file:// URL; mostly it is the same stance as the rest of the
engine — an archive page that phoned home would be tracking readers through the
back door after the mail path refused to track them through the front.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any, Final

from postbag.drip import parse_front_matter
from postbag.render import markdown_to_html

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

_FILENAME_RE: Final = re.compile(r"^(?:\d+)-(.+)$")
# Written into a filename, so anything that could climb a directory or collide
# after normalisation is refused rather than sanitised.
_SAFE_KEY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_PAGE: Final = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>{title}</title></head>"
    '<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
    'line-height:1.6;color:#1a1a1a;max-width:640px;margin:0 auto;padding:24px">'
    "{body}</body></html>"
)


def _field(record: Any, name: str) -> Any:
    """Read `name` from a mapping row or an object row, whichever the host has."""
    try:
        return record[name]
    except (TypeError, KeyError):
        return getattr(record, name)


def _load_messages(messages_dir: Path) -> dict[str, tuple[str, str]]:
    """Map message key -> (subject, body) for every `*.md` in the directory.

    Front matter is read leniently here — the archive renders what was sent, and
    refusing to publish a past send because its file is missing a `day` field
    would make the archive fail on exactly the ad-hoc messages it most wants.
    `drip.load_step` remains strict, because that path decides what to SEND.
    """
    messages: dict[str, tuple[str, str]] = {}
    sources: dict[str, Path] = {}
    for path in sorted(messages_dir.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        match = _FILENAME_RE.match(path.stem)
        key = meta.get("key") or (match.group(1) if match else path.stem)
        if key in sources:
            raise ValueError(
                f"message key {key!r} is used by both {sources[key].name!r} "
                f"and {path.name!r}"
            )
        sources[key] = path
        messages[key] = (meta.get("subject", key), body)
    return messages


def export(
    sends: Iterable[Any],
    messages_dir: Path,
    out_dir: Path,
) -> list[Path]:
    """Write one page per distinct sent `message_key`, plus `index.html`.

    Returns every path written, index last, so a caller can assert on the result
    instead of re-globbing the directory it just asked to be filled.
    """
    first_sent: dict[str, str] = {}
    counts: dict[str, int] = {}
    for send in sends:
        key = str(_field(send, "message_key"))
        sent_at = str(_field(send, "sent_at"))
        counts[key] = counts.get(key, 0) + 1
        if key not in first_sent or sent_at < first_sent[key]:
            first_sent[key] = sent_at

    messages = _load_messages(messages_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    entries: list[tuple[str, str, str]] = []
    for key in sorted(first_sent, key=lambda k: (first_sent[k], k)):
        if not _SAFE_KEY_RE.match(key):
            raise ValueError(f"message key is not safe as a filename: {key!r}")
        subject, body = messages.get(key, (key, ""))
        page = _PAGE.format(
            title=html.escape(subject),
            body=(
                f'<h1 style="font-size:22px">{html.escape(subject)}</h1>'
                '<p style="font-size:13px;color:#666">Sent '
                f"{html.escape(first_sent[key])} "
                f"to {counts[key]} recipient{'' if counts[key] == 1 else 's'}.</p>"
                f"{markdown_to_html(body)}"
                '<p style="font-size:13px"><a href="index.html">All issues</a></p>'
            ),
        )
        path = out_dir / f"{key}.html"
        path.write_text(page, encoding="utf-8")
        written.append(path)
        entries.append((key, subject, first_sent[key]))

    items = "".join(
        f'<li><a href="{html.escape(key, quote=True)}.html">{html.escape(subject)}</a>'
        f' <span style="color:#666">— {html.escape(sent_at)}</span></li>'
        for key, subject, sent_at in entries
    )
    index = _PAGE.format(
        title="Archive",
        body=(
            '<h1 style="font-size:22px">Archive</h1>'
            + (f"<ul>{items}</ul>" if items else "<p>Nothing has been sent yet.</p>")
        ),
    )
    index_path = out_dir / "index.html"
    index_path.write_text(index, encoding="utf-8")
    written.append(index_path)
    return written


def sent_keys(sends: Iterable[Any]) -> Sequence[str]:
    """Distinct message keys in the send log, for callers that only want the list."""
    return sorted({str(_field(send, "message_key")) for send in sends})
