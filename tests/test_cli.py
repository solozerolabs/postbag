"""The CLI surface: exit codes are the contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from postbag.cli import main


def _write_step(
    directory: Path, filename: str, key: str, approved: str = "true"
) -> None:
    (directory / filename).write_text(
        f"---\nkey: {key}\nday: 2\nsubject: S\napproved: {approved}\n---\nBody\n",
        encoding="utf-8",
    )


def test_validate_drip_accepts_a_good_sequence(tmp_path: Path) -> None:
    _write_step(tmp_path, "01-a.md", "a")
    _write_step(tmp_path, "02-b.md", "b", approved="false")
    assert main(["validate-drip", str(tmp_path)]) == 0


def test_validate_drip_exits_one_on_any_error(tmp_path: Path) -> None:
    """Reds if `_validate_drip` returns 0 after collecting errors.

    This command is meant to be a pre-commit gate; a gate that reports problems
    on stderr and still exits 0 is a log line, not a gate.
    """
    _write_step(tmp_path, "01-a.md", "a")
    _write_step(tmp_path, "02-b.md", "wrong-key")
    assert main(["validate-drip", str(tmp_path)]) == 1


def test_validate_drip_reports_every_bad_file_not_just_the_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reds if the loop stops at the first failure — one review pass becomes N."""
    _write_step(tmp_path, "01-a.md", "wrong-one")
    _write_step(tmp_path, "02-b.md", "wrong-two")
    assert main(["validate-drip", str(tmp_path)]) == 1
    assert "2 problem(s)" in capsys.readouterr().err


def test_validate_drip_refuses_an_empty_directory(tmp_path: Path) -> None:
    assert main(["validate-drip", str(tmp_path)]) == 1


def test_allow_empty_permits_an_empty_outbox(tmp_path: Path) -> None:
    """The outbox's resting state. A quiet week writes no issue, and the gate
    that runs every commit must not red on that.
    """
    assert main(["validate-drip", "--allow-empty", str(tmp_path)]) == 0


def test_allow_empty_does_not_excuse_a_broken_file(tmp_path: Path) -> None:
    """The flag widens ONE condition, not the validator.

    Without this, `--allow-empty` on the outbox could quietly become "never
    fail on the outbox" — and the outbox is where sendable copy lives.
    """
    (tmp_path / "00-x.md").write_text("---\nkey: mismatch\n---\nb\n", encoding="utf-8")
    assert main(["validate-drip", "--allow-empty", str(tmp_path)]) == 1


def test_render_weekly_writes_a_draft(tmp_path: Path) -> None:
    entries = tmp_path / "entries.json"
    entries.write_text(
        json.dumps(
            {"entries": [{"date": "2026-08-02", "title": "A thing", "category": "new"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "outbox" / "draft.md"
    assert (
        main(
            [
                "render-weekly",
                "--entries",
                str(entries),
                "--since",
                "2026-08-01",
                "--product",
                "Widgets",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "Widgets: what shipped" in out.read_text(encoding="utf-8")


def test_render_weekly_on_an_empty_window_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reds if a quiet week exits non-zero — that pages someone for nothing."""
    entries = tmp_path / "entries.json"
    entries.write_text(json.dumps({"entries": []}), encoding="utf-8")
    assert (
        main(
            [
                "render-weekly",
                "--entries",
                str(entries),
                "--since",
                "2026-08-01",
                "--product",
                "W",
            ]
        )
        == 0
    )
    assert "nothing to send" in capsys.readouterr().out


def test_export_archive_writes_html(tmp_path: Path) -> None:
    messages = tmp_path / "messages"
    messages.mkdir()
    _write_step(messages, "01-a.md", "a")
    sends = tmp_path / "sends.json"
    sends.write_text(
        json.dumps(
            [{"subscriber_id": "s1", "message_key": "a", "sent_at": "2026-08-01"}]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "site"
    assert (
        main(
            [
                "export-archive",
                "--sends",
                str(sends),
                "--messages-dir",
                str(messages),
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )
    assert (out / "a.html").exists()
    assert (out / "index.html").exists()


def test_require_cutoff_reds_a_calendar_step_without_one(tmp_path: Path) -> None:
    """The gate must refuse exactly what the tick refuses."""
    (tmp_path / "00-weekly.md").write_text(
        "---\nkey: weekly\nday: 0\nsubject: s\napproved: true\n---\nBody\n",
        encoding="utf-8",
    )
    assert main(["validate-drip", "--require-cutoff", str(tmp_path)]) == 1


def test_require_cutoff_passes_when_the_cutoff_is_there(tmp_path: Path) -> None:
    """Negative control: it refuses the missing field, not every calendar file."""
    (tmp_path / "00-weekly.md").write_text(
        "---\nkey: weekly\nday: 0\ncutoff: 2026-08-04T17:30:00+00:00\n"
        "subject: s\napproved: true\n---\nBody\n",
        encoding="utf-8",
    )
    assert main(["validate-drip", "--require-cutoff", str(tmp_path)]) == 0


def test_without_the_flag_a_missing_cutoff_is_fine(tmp_path: Path) -> None:
    """A drip directory is unaffected — day-0 welcomes stay legal."""
    _write_step(tmp_path, "01-a.md", "a")
    assert main(["validate-drip", str(tmp_path)]) == 0
