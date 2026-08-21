"""Rendering: escaping by construction, the mandatory footer, the empty window."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from postbag.render import markdown_to_html, render_changelog_draft, render_email

FOOTER = (
    "Solo Zero Labs, 1 Example St, Springfield. You get this because you signed up."
)
UNSUB = "https://example.test/unsubscribe/abc"


@dataclass(frozen=True)
class Entry:
    date: str
    title: str
    category: str


def test_paragraphs_bullets_and_headings_render() -> None:
    html_body, _ = render_email(
        "Intro line.\n\n**New**\n\n- One\n- Two\n",
        footer=FOOTER,
        unsubscribe_url=UNSUB,
    )
    assert "<p>Intro line.</p>" in html_body
    assert "<strong>New</strong>" in html_body
    assert "<ul><li>One</li><li>Two</li></ul>" in html_body


def test_bare_urls_become_links_in_html_and_stay_bare_in_text() -> None:
    html_body, text_body = render_email(
        "Read https://example.test/post now.", footer=FOOTER, unsubscribe_url=UNSUB
    )
    assert (
        '<a href="https://example.test/post">https://example.test/post</a>' in html_body
    )
    assert "https://example.test/post" in text_body
    assert "<a href" not in text_body


def test_a_bare_url_followed_by_a_period_keeps_the_period_outside_the_link() -> None:
    """The sentence's full stop is not part of the URL and must not join the href.

    `_URL_RE` alone is greedy past `.`, so without the trailing-punctuation
    strip the period lands inside both the href and the visible anchor text.
    """
    html_out = markdown_to_html("Read https://example.test/post.")

    assert (
        '<a href="https://example.test/post">https://example.test/post</a>.'
        in html_out
    )
    assert 'href="https://example.test/post."' not in html_out


def test_a_bare_url_followed_by_a_closing_paren_keeps_the_paren_outside_the_link() -> (
    None
):
    """A URL parenthetically noted, e.g. `(see https://example.test/post)`."""
    html_out = markdown_to_html("See the post (https://example.test/post) for more.")

    assert (
        '<a href="https://example.test/post">https://example.test/post</a>)'
        in html_out
    )
    assert 'href="https://example.test/post)"' not in html_out


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script> shipped",
        "- <script>alert(1)</script> shipped",
        "**<script>alert(1)</script>**",
    ],
    ids=["paragraph", "bullet", "heading"],
)
def test_markup_in_content_is_escaped_in_every_branch(source: str) -> None:
    """Reds if `html.escape` is dropped from `markdown_to_html`.

    Parametrized over all three structural branches on purpose: an earlier
    version escaped in each branch separately, so deleting one call left the
    other two green and the bug shipped. One escape call site, three branches
    that must all route through it.

    Newsletter copy is operator-authored, but the archive republishes it and a
    mail client renders it; a `<script>` that survives rendering is markup the
    engine created out of text, which is the whole class this module refuses to
    have.
    """
    out = markdown_to_html(source)
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_the_footer_is_in_both_parts_always() -> None:
    """Reds if either footer append is removed.

    CAN-SPAM applies to the text/plain part too, and the unsubscribe link is the
    only way off the list.
    """
    html_body, text_body = render_email("Body.", footer=FOOTER, unsubscribe_url=UNSUB)
    assert FOOTER in text_body
    assert UNSUB in text_body
    assert "1 Example St" in html_body
    assert f'href="{UNSUB}"' in html_body


def test_an_empty_body_still_gets_the_footer() -> None:
    """Negative control: the footer is unconditional, not content-driven."""
    html_body, text_body = render_email("", footer=FOOTER, unsubscribe_url=UNSUB)
    assert UNSUB in html_body
    assert UNSUB in text_body


def test_changelog_draft_groups_by_category_and_shapes_the_subject() -> None:
    entries = [
        Entry("2026-08-02", "Faster runs", "improved"),
        Entry("2026-08-03", "New dashboard", "new"),
        Entry("2026-08-01", "Fixed a crash", "fixed"),
    ]
    drafted = render_changelog_draft(
        entries,
        since="2026-08-01",
        until="2026-08-31",
        product="Widgets",
        signature="— Sid",
    )
    assert drafted is not None
    subject, body = drafted
    assert subject == "Widgets: what shipped, 2026-08-01 to 2026-08-03"
    assert body.index("**New**") < body.index("**Improved**") < body.index("**Fixed**")
    assert "- New dashboard" in body
    assert body.rstrip().endswith("— Sid")


def test_an_empty_window_returns_none() -> None:
    """Reds if the empty case returns an empty draft instead of None.

    An empty week is a skip, not a filler email — and None is what makes that
    unmissable at the call site.
    """
    entries = [Entry("2026-07-01", "Old thing", "new")]
    assert (
        render_changelog_draft(
            entries, since="2026-08-01", until="2026-08-31", product="W"
        )
        is None
    )


def test_a_changelog_title_containing_markup_is_escaped_when_rendered() -> None:
    """Reds if the draft body reaches HTML without going through `markdown_to_html`."""
    entries = [Entry("2026-08-02", "<script>alert(1)</script>", "new")]
    drafted = render_changelog_draft(
        entries, since="2026-08-01", until="2026-08-31", product="W"
    )
    assert drafted is not None
    html_body, _ = render_email(drafted[1], footer=FOOTER, unsubscribe_url=UNSUB)
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


def test_a_hard_wrapped_paragraph_renders_as_one_paragraph() -> None:
    """Prose is hard-wrapped; a reader must not see a break after every line.

    Real drip copy wraps at ~78 columns, as prose should. Rendering a <p> per
    SOURCE LINE put a paragraph break mid-sentence — "Here is what that means,"
    and "plainly." arrived as two paragraphs. Every unit test passed and
    every gate was green; it was only visible by rendering a real file and
    looking at the output.

    Mutation: emit per-line instead of buffering -> this reds.
    """
    html = markdown_to_html(
        "You signed up a couple of days ago. Here is what that means,\n"
        "plainly.\n"
        "\n"
        "A second paragraph, also wrapped\n"
        "across two lines.\n"
    )

    assert html.count("<p>") == 2, (
        f"expected 2 paragraphs, got {html.count('<p>')}: {html}"
    )
    assert "means, plainly." in html, "the wrapped lines were not joined with a space"


def test_bold_becomes_markup_not_literal_asterisks() -> None:
    """`**text**` must reach the inbox as emphasis, not as asterisks.

    Both committed drip files use `**` for their lead-ins, so before this every
    subscriber would have read "**What happens next.**" with the markers showing.
    """
    html = markdown_to_html("**What happens next.** Admission is in waves.")

    assert "<strong>What happens next.</strong>" in html
    assert "**" not in html


def test_emphasis_cannot_smuggle_markup_through_the_escape() -> None:
    """The negative control, and the reason `_emphasis` runs AFTER `html.escape`.

    Author content is neutralised before the emphasis pass, so the only angle
    brackets it can introduce are its own. A `**` wrapper around markup must
    emphasise the ESCAPED text, never revive it.
    """
    html = markdown_to_html("**<script>alert(1)</script>**")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>" in html


def test_a_bolded_url_keeps_a_working_href() -> None:
    """Emphasis runs BEFORE linkify, and the order is the whole finding.

    Linkify-first swallowed the closing `**` into the URL match (`_URL_RE` does
    not exclude `*`), then the emphasis pass rewrote inside the attribute:
    `href="https://x/a</strong>"` — a dead link whose visible text still showed
    the asterisks. Not an escaping hole, since `"` is already `&quot;` by then,
    but a broken link in a product email is a broken product.

    Mutation: swap to `_emphasis(_linkify(...))` -> this reds.
    """
    html_out = markdown_to_html("Visit **https://example.test/join** to join.")

    assert '<a href="https://example.test/join">' in html_out
    assert '</strong>"' not in html_out, "the closing tag leaked into the href"
    assert "**" not in html_out


def test_bold_spanning_a_hard_wrap_still_renders() -> None:
    """Inline markup runs on the JOINED paragraph, not per source line.

    Copy wraps at ~78 columns, so a bold span crossing the wrap would otherwise
    ship its asterisks literally — the same defect this renderer was fixed for,
    surviving in exactly the case that wraps.

    Mutation: run the inline pass per line instead of in flush_para -> this reds.
    """
    html_out = markdown_to_html("wrapped **bold that\nspans the wrap** end of it.")

    assert "<strong>bold that spans the wrap</strong>" in html_out
    assert "**" not in html_out


def test_the_text_part_shows_no_markdown_markers() -> None:
    """A plain-text reader must not see `**` — the HTML fix, one alternative over.

    Mutation: skip the emphasis strip in `_to_text` -> this reds.
    """
    _, text_body = render_email(
        "**You write a spec.** Plain prose follows.",
        footer=FOOTER,
        unsubscribe_url=UNSUB,
    )

    assert "**" not in text_body
    assert "You write a spec. Plain prose follows." in text_body


def test_the_text_part_strips_bold_that_crosses_a_hard_wrap() -> None:
    """A `**bold**` span crossing the 78-column hard wrap must not ship markers.

    Both parts now segment through one `_segment`, which joins a paragraph's
    wrapped lines before emphasis runs — so the span is stripped in text exactly
    as it renders in HTML. The plain-text paragraph is flowing prose (clients
    reflow it); the markers are gone.

    Mutation: emit a `<p>` / text line per source line instead of joining ->
    this reds.
    """
    _, text_body = render_email(
        "This lead is **bold that spans\nthe hard wrap** across lines.",
        footer=FOOTER,
        unsubscribe_url=UNSUB,
    )
    body_part = text_body.split("\n\n---")[0]

    assert "**" not in body_part
    assert "bold that spans the hard wrap" in body_part  # markers gone, lines joined


def test_both_parts_share_one_segmentation() -> None:
    """The two MIME parts derive marker survival from ONE `_segment` + ONE regex.

    Four rounds each found a case where the HTML and text parts had drifted on
    what counts as a paragraph or a bold span, because each kept its own
    definition. The fix is structural: both consume `_to_text`/`markdown_to_html`
    off the same `_segment`, and emphasis is the same `_EMPHASIS_RE` run on the
    same per-block joined content — `html.escape` never touches `*`, so a
    WELL-FORMED `**pair**` is stripped in text exactly when it renders in HTML.

    This asserts that identity over the input the content pipeline can actually
    produce: balanced `**pair**` spans in reviewed, approval-gated copy. It does
    NOT assert universal agreement on malformed asterisk soup (`**` runs of 3+):
    there, HTML's `<strong>` tags cosmetically separate stray asterisks that bare
    text re-adjoins, the HTML is itself broken (`x<strong>*</strong>*`), and no
    authored copy contains it — a false universal claim here was the round-11
    finding.

    Mutation: give the text renderer its own segmentation (e.g. split on `\n`
    only, not `_segment`) -> a `\r\r` or wrapped case reds.
    """
    from postbag.render import _segment, _to_text, markdown_to_html

    cases = [
        "**bold** in a line",
        "wrapped **span that\ncrosses** a hard wrap",
        "line one\rline two **bold**",  # \r is a splitlines() boundary
        "para one **a**\n\npara two **b**",
        "- **bold bullet** item",
        "**A Heading**\n\nbody **bold** here",
        "5 ** 3 is arithmetic, not bold",  # unmatched-in-prose stays put in both
    ]
    for body in cases:
        html_has = "**" in markdown_to_html(body)
        text_has = "**" in _to_text(body)
        assert html_has == text_has, (
            f"parts disagree on well-formed {body!r}: html={html_has} text={text_has}"
        )
    # the segmenter itself is the shared source of truth
    # a single \r is a line boundary (lines JOIN into one paragraph); \r\r is a
    # blank line (paragraphs SPLIT). Both handled by the one splitlines()-based
    # segmenter, which is exactly why the text part no longer drifts from HTML.
    assert _segment("a\rb") == [("paragraph", "a b")]
    assert _segment("a\r\rb") == [("paragraph", "a"), ("paragraph", "b")]


def test_bullet_runs_match_the_pre_unification_renderer() -> None:
    """A blank line breaks a bullet run; consecutive bullets are one list.

    The unification's first cut lost this: `_segment` treated each bullet as its
    own block, so blank-separated bullets (a loose list) coalesced into one
    `<ul>` and tight-list plain text gained a blank line between items — a
    well-formed-input divergence a verification round caught. `_segment` now
    groups a contiguous run, matching the renderer this replaced byte-for-byte.

    Mutation: emit one bullet block per item instead of a run -> this reds.
    """
    from postbag.render import _to_text

    # tight: one list, both parts
    assert markdown_to_html("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"
    assert _to_text("- a\n- b") == "- a\n- b"
    # loose: a blank line breaks the run -> two lists / two text blocks
    assert markdown_to_html("- a\n\n- b") == "<ul><li>a</li></ul><ul><li>b</li></ul>"
    assert _to_text("- a\n\n- b") == "- a\n\n- b"
    # a non-bullet line also breaks the run
    assert markdown_to_html("- a\ntext\n- b").count("<ul>") == 2
