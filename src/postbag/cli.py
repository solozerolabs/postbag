"""The command line: render, export, validate.

Nothing that sends, and nothing that writes to a database.

v1 has no database subcommands ON PURPOSE. The host owns the connection, the
credentials, and the transaction boundary; a CLI that opened its own connection
would need its own copy of all three, and the first person to run it against the
wrong `DATABASE_URL` would find out at the same moment their subscribers did.

Nothing here sends mail either. `validate-drip` is the pre-commit check on the
approve-once seam, and `render-weekly` writes a draft a human then reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from postbag.archive import export
from postbag.drip import DripError, load_step
from postbag.render import render_changelog_draft


@dataclass(frozen=True, slots=True)
class _Entry:
    """The changelog shape `render_changelog_draft` reads, parsed from JSON."""

    date: str
    title: str
    category: str


def _load_entries(path: Path) -> list[_Entry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload["entries"] if isinstance(payload, dict) else payload
    return [
        _Entry(date=str(e["date"]), title=str(e["title"]), category=str(e["category"]))
        for e in raw
    ]


def _render_weekly(args: argparse.Namespace) -> int:
    entries = _load_entries(Path(args.entries))
    drafted = render_changelog_draft(
        entries,
        since=args.since,
        until=args.until,
        product=args.product,
        signature=args.signature,
    )
    if drafted is None:
        # An empty window is a skip, not a filler email — and therefore a
        # SUCCESS. Exiting non-zero here would make a quiet week page someone.
        print(f"no entries between {args.since} and {args.until} — nothing to send.")
        return 0
    subject, body = drafted
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"---\nsubject: {subject}\n---\n{body}", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    print(f"subject: {subject}\n")
    print(body, end="")
    return 0


def _export_archive(args: argparse.Namespace) -> int:
    sends = json.loads(Path(args.sends).read_text(encoding="utf-8"))
    written = export(sends, Path(args.messages_dir), Path(args.out_dir))
    print(f"wrote {len(written)} files to {args.out_dir}")
    return 0


def _validate_drip(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return 1
    files = sorted(directory.glob("*.md"))
    if not files:
        # An empty DRIP directory is a mistake worth failing on — someone
        # pointed the tick at the wrong path. An empty OUTBOX is the normal
        # resting state: calendar issues are written when there is news, and a
        # week with none writes nothing. Same validator, opposite default.
        if args.allow_empty:
            print(f"no .md files in {directory} (allowed).")
            return 0
        print(f"no .md files in {directory}", file=sys.stderr)
        return 1

    # Every file is checked before reporting: a validator that stops at the
    # first error turns one review pass into N.
    errors: list[str] = []
    keys: dict[str, str] = {}
    approved = 0
    for path in files:
        try:
            step = load_step(path)
        except DripError as exc:
            errors.append(str(exc))
            continue
        if step.key in keys:
            errors.append(
                f"{path.name}: duplicate key {step.key!r} (also in {keys[step.key]})"
            )
        keys[step.key] = path.name
        approved += int(step.approved)
        # The same rule the runtime enforces via `load_sequence(require_cutoff=)`.
        # Checked here too so the gate refuses exactly what the tick refuses —
        # a calendar step with no cutoff mails every future subscriber, and
        # finding that at send time is finding it too late.
        if args.require_cutoff and step.cutoff is None:
            errors.append(
                f"{path.name}: calendar steps must declare a `cutoff`, or they "
                "mail every future subscriber"
            )

    for error in errors:
        print(f"  {error}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} problem(s) in {directory}", file=sys.stderr)
        return 1
    print(f"{len(files)} step(s) OK in {directory}; {approved} approved to send.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postbag", description=__doc__.splitlines()[0]
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    weekly = subparsers.add_parser(
        "render-weekly", help="render a changelog window into a draft"
    )
    weekly.add_argument(
        "--entries", required=True, help="path to a changelog entries JSON file"
    )
    weekly.add_argument(
        "--since", required=True, help="inclusive ISO date, e.g. 2026-08-01"
    )
    weekly.add_argument("--until", default="9999-12-31", help="inclusive ISO date")
    weekly.add_argument(
        "--product", required=True, help="product name used in the subject line"
    )
    weekly.add_argument("--signature", default=None, help="sign-off line, e.g. '— Sid'")
    weekly.add_argument(
        "--out", default=None, help="write the draft here as well as stdout"
    )
    weekly.set_defaults(func=_render_weekly)

    archive = subparsers.add_parser(
        "export-archive", help="write static HTML from a send log"
    )
    archive.add_argument(
        "--sends", required=True, help="path to a JSON array of send rows"
    )
    archive.add_argument(
        "--messages-dir", required=True, help="directory of message markdown files"
    )
    archive.add_argument(
        "--out-dir", required=True, help="directory to write HTML into"
    )
    archive.set_defaults(func=_export_archive)

    drip = subparsers.add_parser(
        "validate-drip", help="check a drip sequence directory"
    )
    drip.add_argument("directory", help="directory of NN-key-name.md files")
    drip.add_argument(
        "--allow-empty",
        action="store_true",
        help="an empty directory is OK (the outbox, where a quiet week is normal)",
    )
    drip.add_argument(
        "--require-cutoff",
        action="store_true",
        help="every step must declare a cutoff (a calendar/outbox directory)",
    )
    drip.set_defaults(func=_validate_drip)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
