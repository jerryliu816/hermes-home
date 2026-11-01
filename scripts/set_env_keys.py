#!/usr/bin/env python3
"""Set KEY=VALUE pairs in a dotenv file, writing the result to a new file.

    set_env_keys.py SOURCE DEST KEY=VALUE [KEY=VALUE ...]

Every other line of SOURCE is copied unchanged, comments and secrets
included. A key already present is replaced in place (a duplicate later line
for the same key is dropped); a key not present is appended. DEST is written
with mode 0600 and replaced atomically, so a half-written secrets file can
never exist.

It prints key names and the new values it set -- never any value it did not
set itself, so the rest of the file (tokens, keys, secrets) stays unseen.
Used by scripts/vision.sh; deliberately standard-library only so it runs with
the system python3 on the host.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_keys(text: str, changes: dict[str, str]) -> tuple[str, list[str]]:
    """Return the new file text and a report of what was set."""
    seen: set[str] = set()
    out: list[str] = []
    report: list[str] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        key = match.group(1) if match else None
        if key in changes:
            if key in seen:
                report.append(f"{key}: duplicate line removed")
                continue
            seen.add(key)
            out.append(f"{key}={changes[key]}")
            report.append(f"{key}={changes[key]}")
        else:
            out.append(line)
    missing = [k for k in changes if k not in seen]
    if missing:
        out.append("")
        out.append("# ---- set by scripts/vision.sh ----")
        for key in missing:
            out.append(f"{key}={changes[key]}")
            report.append(f"{key}={changes[key]} (added)")
    return "\n".join(out) + "\n", report


def parse_changes(args: list[str]) -> dict[str, str]:
    changes: dict[str, str] = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep or not _KEY.match(key):
            raise ValueError(f"expected KEY=VALUE, got {key!r}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"value for {key} contains a newline")
        changes[key] = value
    return changes


def write_private(dest: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    source, dest = Path(argv[1]), Path(argv[2])
    try:
        changes = parse_changes(argv[3:])
    except ValueError as exc:
        print(f"set_env_keys: {exc}", file=sys.stderr)
        return 2
    text, report = set_keys(source.read_text(encoding="utf-8"), changes)
    write_private(dest, text)
    for line in report:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
