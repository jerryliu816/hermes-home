"""No committed command may open the live database from the host.

This is a repository-level guardrail rather than a unit test, because the
failure it prevents is operational and silent.

Measured on this deployment: with the container committing one row per second
to the WAL database on the macOS bind mount, a host-side reader saw a frozen
row count across five consecutive reads, and 16 committed rows were permanently
lost -- gone from the writer's own connection, with no error raised anywhere.
Sixty commits reported success; forty-four survived.

The bind mount alone is fine; the container wrote to it for hours. The hazard is
*concurrent host access*, because the WAL index lives in shared memory that
virtiofs does not carry across the host/VM boundary. So the rule is simple and
absolute: live-database SQLite access happens inside the container.

Copies are exempt -- a finished backup or snapshot has no live WAL and nothing
else holds it, which is exactly what makes them the supported way to analyze
data on the host.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Files an operator might copy a command out of.
SCANNED = ("Makefile", "*.sh", "*.md", "*.yml", "*.yaml")

#: The live database, by any path an operator would type on the HOST.
#:
#: Deliberately excludes the container-absolute "/data/..." -- a command using
#: that path is running inside the container, which is the safe case.
LIVE_HOST_PATHS = (
    re.compile(r"(?<![\w/])(?:\./)?data/hermes-home\.db"),
    re.compile(r"\$\(DB\)"),
    re.compile(r"\$DB\b"),  # not $DB_PATH, which is container-absolute
)

#: An explicit, reasoned exemption. Written on the line itself so the
#: justification travels with the command it excuses.
EXEMPT = "host-db-ok"

#: A command runs in the container when it is routed through docker.
IN_CONTAINER = ("docker exec", "docker compose exec", "$(COMPOSE) exec", "docker-compose exec")


def _files() -> list[Path]:
    found: list[Path] = []
    for pattern in SCANNED:
        found.extend(REPO.glob(pattern))
        for path in REPO.glob(f"*/{pattern}"):
            # docker/ runs inside the container by construction, and data/ is
            # the database itself, not instructions anyone follows.
            if not {".venv", "data", "docker"} & set(path.relative_to(REPO).parts):
                found.append(path)
    return sorted(set(found))


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    offences: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_fence = False
    markdown = path.suffix == ".md"

    for number, line in enumerate(lines, 1):
        stripped = line.strip()

        if markdown:
            # Only commands count. Prose *about* the rule -- including the
            # warning telling people not to do this -- is not a violation.
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
        elif stripped.startswith("#") or re.match(r"^[\w.-]+:.*##", stripped):
            # A comment, or a Makefile target's own help text.
            continue

        if EXEMPT in line:
            continue
        if not re.search(r"\bsqlite3\b", stripped):
            continue
        if any(marker in stripped for marker in IN_CONTAINER):
            continue
        if any(pattern.search(stripped) for pattern in LIVE_HOST_PATHS):
            offences.append((number, stripped))
    return offences


def test_no_committed_command_opens_the_live_database_from_the_host() -> None:
    problems: list[str] = []
    for path in _files():
        for number, line in _offending_lines(path):
            problems.append(f"{path.relative_to(REPO)}:{number}: {line}")

    assert not problems, (
        "these run sqlite3 against the LIVE database from the host, which has been "
        "measured silently destroying committed transactions:\n  "
        + "\n  ".join(problems)
        + "\n\nRoute them through the container (docker compose exec ... sqlite3), or "
        "point them at a finished snapshot: make db-snapshot"
    )


def test_the_supported_commands_exist() -> None:
    """An operator told not to do the unsafe thing needs the safe thing to hand."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    for target in ("db-shell:", "db-check:", "db-query:", "db-snapshot:", "backup:"):
        assert target in makefile, f"missing supported command: make {target.rstrip(':')}"


def test_the_supported_db_commands_all_run_in_the_container() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("db-shell:", 1)[1].split("\nshell:", 1)[0]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or re.match(r"^[\w.-]+:.*##", stripped):
            continue  # a comment, or the target's own help text
        if re.search(r"\bsqlite3\b", stripped):
            assert any(m in stripped for m in IN_CONTAINER), (
                f"live-database access must run in the container: {stripped}"
            )


def test_snapshot_script_takes_its_copy_inside_the_container() -> None:
    """The host may read a finished copy; it must not make one from the live file."""
    script = (REPO / "scripts" / "snapshot.sh").read_text(encoding="utf-8")
    assert "docker exec" in script
    backup_line = next(
        line
        for line in script.splitlines()
        if ".backup" in line and not line.strip().startswith("#")
    )
    assert "docker exec" in backup_line, "the .backup must be taken in-container"


def test_restore_refuses_to_run_against_a_live_service() -> None:
    """Restore touches the host file directly, so nothing else may hold it."""
    script = (REPO / "scripts" / "restore.sh").read_text(encoding="utf-8")
    assert "ps --status running" in script
    assert "make stop" in script
