"""Archive export: one page per sent key, an index, and nothing external."""

from __future__ import annotations

from pathlib import Path

import pytest

from postbag.archive import export, sent_keys


def _messages(tmp_path: Path) -> Path:
    directory = tmp_path / "messages"
    directory.mkdir()
    (directory / "01-what-it-is.md").write_text(
        "---\nkey: what-it-is\nday: 2\nsubject: What it is\napproved: true\n---\n"
        "Intro paragraph.\n\n- A bullet\n",
        encoding="utf-8",
    )
    return directory


def test_export_writes_a_page_per_sent_key_plus_an_index(tmp_path: Path) -> None:
    out = tmp_path / "out"
    sends = [
        {
            "subscriber_id": "s1",
            "message_key": "what-it-is",
            "sent_at": "2026-08-01T00:00:00Z",
        },
        {
            "subscriber_id": "s2",
            "message_key": "what-it-is",
            "sent_at": "2026-08-02T00:00:00Z",
        },
    ]
    written = export(sends, _messages(tmp_path), out)

    assert [p.name for p in written] == ["what-it-is.html", "index.html"]
    page = (out / "what-it-is.html").read_text(encoding="utf-8")
    assert "What it is" in page
    assert "Intro paragraph." in page
    assert "<li>A bullet</li>" in page
    # Earliest send wins the dateline, and both recipients are counted.
    assert "2026-08-01T00:00:00Z" in page
    assert "2 recipients" in page
    assert 'href="what-it-is.html"' in (out / "index.html").read_text(encoding="utf-8")


def test_a_drafted_but_unsent_message_gets_no_page(tmp_path: Path) -> None:
    """Reds if `export` iterates the messages directory instead of the send log.

    The send log is the evidence that mail went out; a page for an unsent draft
    would publish copy nobody approved for publication.
    """
    out = tmp_path / "out"
    written = export([], _messages(tmp_path), out)
    assert [p.name for p in written] == ["index.html"]
    assert "Nothing has been sent yet" in (out / "index.html").read_text(
        encoding="utf-8"
    )


def test_pages_reference_no_external_assets(tmp_path: Path) -> None:
    """Reds if a stylesheet, font, or script tag is added.

    An archive page that phoned home would track readers through the back door
    after the mail path refused to track them through the front.
    """
    out = tmp_path / "out"
    sends = [
        {"subscriber_id": "s1", "message_key": "what-it-is", "sent_at": "2026-08-01"}
    ]
    for path in export(sends, _messages(tmp_path), out):
        text = path.read_text(encoding="utf-8")
        assert "<script" not in text
        assert "<link" not in text
        assert "http://" not in text


def test_a_message_key_that_is_unsafe_as_a_filename_is_refused(tmp_path: Path) -> None:
    """Reds if the key is written straight into a path — `../` would escape out_dir."""
    with pytest.raises(ValueError, match="not safe as a filename"):
        export(
            [{"message_key": "../../etc/passwd", "sent_at": "2026-08-01"}],
            _messages(tmp_path),
            tmp_path / "out",
        )


def test_sent_keys_are_distinct_and_sorted() -> None:
    sends = [
        {"message_key": "b", "sent_at": "1"},
        {"message_key": "a", "sent_at": "2"},
        {"message_key": "b", "sent_at": "3"},
    ]
    assert sent_keys(sends) == ["a", "b"]
