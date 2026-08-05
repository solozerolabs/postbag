"""The host-independence boundary, asserted rather than promised.

This package is developed inside its host's monorepo and published as a mirror.
That arrangement is only safe if the code cannot quietly acquire a reference to
the host — a config default, an example, a stray comment, a schema name baked
into a string. So the check runs in the package's own test suite, where a
violation reds before it can ship, rather than in a publish script nobody runs
until release day.

The needle is assembled from fragments on purpose: spelling it out would make
this file the first thing its own scan finds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

PACKAGE_ROOT: Final = Path(__file__).resolve().parents[1]

# The host's name, never written whole anywhere in this tree.
# Split so this file does not itself contain the string it forbids.
#
# The second needle is here because the host NAME was never the whole leak: four
# passages shipped host VOCABULARY (a queue-for-early-access concept this
# package knows nothing about) through a scan that only looked for the company
# name. A published package describing its consumer's domain is the same defect
# one word over, and two of those four were factually wrong by then as well.
# Both needles are split so this file does not trip its own scan.
_NEEDLES: Final = ("synd" + "ai", "wait" + "list")

_SKIP_DIRS: Final = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".ruff_cache",
        "dist",
        "build",
    }
)
_SKIP_SUFFIXES: Final = frozenset(
    {".pyc", ".so", ".whl", ".png", ".jpg", ".gif", ".ico"}
)


def _scannable_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_host_reference_anywhere_in_the_package() -> None:
    """Reds the moment a host name lands in any source, doc, or test file here.

    Case-insensitive, and it covers the whole package rather than just `src`,
    because a README or a test fixture is published by the mirror too.
    """
    offenders = []
    for path in _scannable_files(PACKAGE_ROOT):
        haystack = path.read_text(encoding="utf-8", errors="ignore").lower()
        hits = [n for n in _NEEDLES if n in haystack or n in path.name.lower()]
        if hits:
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)} ({', '.join(hits)})")
    assert offenders == [], f"host reference found in: {offenders}"


def test_the_scan_actually_reads_files(tmp_path: Path) -> None:
    """Negative control: proves the scan is not passing because it read nothing.

    Without this, deleting the body of `_scannable_files` would leave the
    boundary test green forever — the exact all-skipped false-pass shape.
    """
    assert len(_scannable_files(PACKAGE_ROOT)) >= 10

    planted = tmp_path / "planted.py"
    planted.write_text(f"# {_NEEDLES[0]} lives here\n", encoding="utf-8")
    found = [
        p
        for p in _scannable_files(tmp_path)
        if _NEEDLES[0] in p.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    assert found == [planted]


def test_the_engine_imports_no_database_driver() -> None:
    """Reds if sqlalchemy/asyncpg/psycopg/structlog/yaml is imported by the engine.

    The dependency-free store is the reason a host can adopt this without
    surrendering its connection pool or its transaction boundaries. httpx is the
    single allowed third-party import, and only in the transport.
    """
    banned = (
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "structlog",
        "yaml",
        "markdown",
        "jinja2",
    )
    # Anchored at the start of a statement so `from postbag.render import
    # markdown_to_html` is not mistaken for a markdown dependency.
    pattern = re.compile(rf"^\s*(?:import|from)\s+({'|'.join(banned)})\b", re.MULTILINE)
    offenders = [
        f"{path.name}: {match}"
        for path in (PACKAGE_ROOT / "src").rglob("*.py")
        for match in pattern.findall(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    # Negative control: the pattern does find a real import when there is one.
    assert pattern.findall("import yaml\n") == ["yaml"]
