"""Email rendering: a deliberately tiny markdown subset, escaped first.

There is no markdown library here, and that is a decision rather than an
omission. A full markdown renderer accepts raw HTML by design — the feature is
the vulnerability — so using one in a mail path means auditing an escaping
configuration on every upgrade. What newsletter copy actually needs is
paragraphs, bullets, bold headings, and links. That is fifty lines, and it can
be made safe by construction:

EVERYTHING IS `html.escape`d BEFORE ANY MARKUP IS ADDED. The escaping discipline
follows the common shell pattern, where the invariant is usually stated as
"body_html must ALREADY be escaped by the caller (it is markup by definition)".
Here the invariant is stronger and needs no caller cooperation:
the source text is escaped on the way in, and every tag this module emits is a
literal in this file. There is no path by which caller text becomes markup.

The footer — postal address plus the sentence saying which list this is — is
appended to BOTH parts, always. CAN-SPAM applies to the plain-text part too,
which is the sort of thing that gets remembered by the person who once shipped
a text/plain body without one.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

# Bare URLs in the source text. Applied to text that is ALREADY escaped, so the
# match cannot contain `<`, `"` or `&` in raw form — `&` arrives as `&amp;`,
# which is exactly what an href attribute wants.
_URL_RE: Final = re.compile(r"(https?://[^\s<)\]]+)")

# Sentence-ending punctuation trailing a matched URL: not part of the link,
# just the author's sentence continuing. Stripped off the END of a match only,
# so a `?` or `:` that is part of the URL itself (a query string, a port) is
# untouched — only a run of these characters at the very tail is punctuation.
_TRAILING_PUNCTUATION_RE: Final = re.compile(r"[.,;:!?]+$")

# A whole line wrapped in `**`: the only heading form. `#` is not supported
# because mail clients render nothing special for it and copy that uses it in a
# newsletter almost always means "bold line".
_HEADING_RE: Final = re.compile(r"^\*\*(.+)\*\*$")

_HEADINGS: Final = (("new", "New"), ("improved", "Improved"), ("fixed", "Fixed"))


class ChangelogEntry(Protocol):
    """Structural type, so the engine never imports the host's changelog module."""

    @property
    def date(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def category(self) -> str: ...


_EMPHASIS_RE: Final = re.compile(r"\*\*(.+?)\*\*")


def _emphasis(escaped: str) -> str:
    """`**text**` -> `<strong>text</strong>`, on an ALREADY-ESCAPED string.

    Applied after `html.escape` on purpose: every `<` from the author is already
    `&lt;` by this point, so the only angle brackets this can introduce are the
    ones written here. That keeps the module's one-escape rule intact — the
    branch decides structure, never safety.

    Before this existed the markers shipped LITERALLY. The two committed drip
    files both use `**` for their lead-ins, so every subscriber would have read
    "**What happens next.**" with the asterisks showing.
    """
    return _EMPHASIS_RE.sub(r"<strong>\1</strong>", escaped)


def _linkify(escaped: str) -> str:
    """Wrap bare URLs in anchors. Input is already escaped; output is markup.

    A trailing period, comma, semicolon, colon, exclamation mark, or question
    mark is sentence punctuation, not part of the link, and is moved outside
    the anchor rather than swallowed into the href and visible text.
    """

    def _replace(match: re.Match[str]) -> str:
        url = match.group(1)
        trailing = _TRAILING_PUNCTUATION_RE.search(url)
        punctuation = trailing.group(0) if trailing else ""
        if punctuation:
            url = url[: -len(punctuation)]
        return f'<a href="{url}">{url}</a>{punctuation}'

    return _URL_RE.sub(_replace, escaped)


def _inline(escaped: str) -> str:
    """Emphasis THEN links, on already-escaped text. The order is load-bearing.

    Linkify-first corrupted the href: `_URL_RE` does not exclude `*`, so the
    closing `**` of a bolded URL was swallowed into the URL match, and the
    emphasis pass then rewrote INSIDE the attribute — `**https://x/a**`
    produced `href="https://x/a</strong>"`, a dead link whose visible text
    still showed the asterisks. Emphasis first cannot do that: `_URL_RE`
    excludes `<`, so a `<strong>` next to a URL cleanly ends the match.

    Not an escaping hole either way — `"` is already `&quot;` by here, so
    author text can never terminate the attribute — but a broken link in a
    product-update email is a broken product.
    """
    return _linkify(_emphasis(escaped))


def _segment(body: str) -> list[tuple[str, object]]:
    """The ONE segmentation both MIME parts share: (kind, content).

    `kind` is "paragraph" (content = joined line str) | "bullet" (content = list
    of item strs for ONE contiguous run) | "heading" (content = str). Grouping
    the whole document here — including collapsing a paragraph's wrapped lines,
    and gathering a bullet RUN that a blank line or a non-bullet ends — is what
    lets the HTML and text consumers stay byte-faithful to each other and to the
    pre-unification renderer. Four review rounds found a case where two separate
    markdown passes had drifted (a wrapped bold, a `\r\r` break only
    `splitlines()` sees, a loose-vs-tight bullet list); one segmenter removes it.
    """
    blocks: list[tuple[str, object]] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(("paragraph", " ".join(para)))
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            blocks.append(("bullet", list(bullets)))
            bullets.clear()

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush_para()
            flush_bullets()
            continue
        heading = _HEADING_RE.match(line)
        if line.startswith("- "):
            flush_para()
            bullets.append(line[2:].strip())
        elif heading:
            flush_para()
            flush_bullets()
            blocks.append(("heading", heading.group(1)))
        else:
            flush_bullets()
            para.append(line)
    flush_para()
    flush_bullets()
    return blocks


def markdown_to_html(body: str) -> str:
    """Render the supported subset to HTML fragments.

    THERE IS EXACTLY ONE `html.escape` CALL BELOW, and every branch reaches the
    output through it. An earlier version escaped separately in the paragraph,
    bullet, and heading branches; deleting any one of the three left the other
    two passing, which is the "one normalization re-derived at three sites"
    shape — the branch decides the STRUCTURE, never the safety.

    Segmentation is `_segment`, shared with the text part so the two cannot
    drift on what counts as a paragraph or a bold span.
    """
    out: list[str] = []

    for kind, content in _segment(body):
        if kind == "bullet":
            items = "".join(
                f"<li>{_inline(html.escape(item))}</li>" for item in content
            )
            out.append(f"<ul>{items}</ul>")
            continue
        rendered = _inline(html.escape(content))
        if kind == "heading":
            out.append(f'<p style="margin:16px 0 4px"><strong>{rendered}</strong></p>')
        else:
            out.append(f"<p>{rendered}</p>")
    return "".join(out)


def _html_shell(*, body_html: str, footer: str, unsubscribe_url: str) -> str:
    """The one HTML envelope every message uses.

    One envelope, because the duplicated part is the compliance footer, and a
    compliance footer that exists in two places is a footer that eventually
    exists correctly in one.
    """
    return (
        "<!doctype html><html><body "
        'style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'line-height:1.6;color:#1a1a1a;max-width:520px;margin:0 auto;padding:24px">'
        f"{body_html}"
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        '<p style="font-size:12px;color:#666">'
        f'<a href="{html.escape(unsubscribe_url, quote=True)}">Unsubscribe</a>'
        f"<br>{html.escape(footer)}</p>"
        "</body></html>"
    )


def _to_text(body: str) -> str:
    """The plain-text body, from the SAME `_segment` blocks the HTML uses.

    `**` markers are dropped with the SAME `_EMPHASIS_RE` the HTML side runs
    (via `_emphasis`) on the SAME per-block joined content, so a marker survives
    in the text part exactly when it survives in HTML — `html.escape` never
    touches `*`, so escaping cannot change the match. That identity is what four
    rounds of patching two separate regexes could not achieve; it is now
    structural, not asserted. Paragraphs read as flowing prose (clients reflow
    plain text), bullets keep their dash, headings stand on their own line.
    """
    blocks: list[str] = []
    for kind, content in _segment(body):
        if kind == "bullet":
            blocks.append(
                "\n".join("- " + _EMPHASIS_RE.sub(r"\1", item) for item in content)
            )
        else:
            blocks.append(_EMPHASIS_RE.sub(r"\1", content))
    return "\n\n".join(blocks)


def _text_footer(*, footer: str, unsubscribe_url: str) -> str:
    """The plain-text counterpart. CAN-SPAM applies to both parts, not just the HTML."""
    return f"---\nUnsubscribe: {unsubscribe_url}\n{footer}\n"


def render_email(
    markdown_body: str,
    *,
    footer: str,
    unsubscribe_url: str,
) -> tuple[str, str]:
    """Return `(html_body, text_body)` for one message.

    `footer` is the postal address plus the one-sentence statement of which list
    this is and why the reader is on it. Both are appended unconditionally —
    there is no flag to omit them, because the only reason to add such a flag is
    to break the law with less typing.
    """
    html_body = _html_shell(
        body_html=markdown_to_html(markdown_body),
        footer=footer,
        unsubscribe_url=unsubscribe_url,
    )
    text_body = (
        _to_text(markdown_body)
        + "\n\n"
        + _text_footer(footer=footer, unsubscribe_url=unsubscribe_url)
    )
    return html_body, text_body


def render_changelog_draft(
    entries: Sequence[ChangelogEntry],
    *,
    since: str,
    until: str,
    product: str,
    signature: str | None = None,
) -> tuple[str, str] | None:
    """Return `(subject, markdown_body)` for a changelog window, or None if empty.

    AN EMPTY WINDOW IS A SKIP, NOT A FILLER EMAIL. Returning None rather than an
    empty body is what makes that unmissable at the call site: a weekly cadence
    with nothing to say produces the one thing that kills a list faster than
    silence, which is mail that wastes the reader's time, on a schedule.

    `product` and `signature` are parameters rather than constants because this
    module ships in a library that has no product of its own; the subject shape
    and the New/Improved/Fixed grouping are the host script's, unchanged.
    """
    window = [e for e in entries if since <= e.date <= until]
    if not window:
        return None

    subject = f"{product}: what shipped, {since} to {max(e.date for e in window)}"
    lines = [f"Here is what changed in {product} this week.", ""]
    for key, heading in _HEADINGS:
        bucket = [e for e in window if e.category == key]
        if not bucket:
            continue
        lines.append(f"**{heading}**")
        lines.append("")
        lines.extend(
            f"- {e.title}" for e in sorted(bucket, key=lambda e: e.date, reverse=True)
        )
        lines.append("")
    if signature:
        lines.append(signature)
    return subject, "\n".join(lines).rstrip() + "\n"
