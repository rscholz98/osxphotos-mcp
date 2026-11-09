"""Run osxphotos CLI commands for the model, minus the parts that are unsafe to hand over."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

READ_COMMANDS = frozenset(
    {
        "about",
        "albums",
        "compare",
        "dbversion",
        "dump",
        "exiftool",
        "export",
        "exportdb",
        "info",
        "keywords",
        "labels",
        "list",
        "orphans",
        "persons",
        "places",
        "query",
        "show",
        "uuid",
        "version",
    }
)
"""Commands that never change the Photos library."""

WRITE_COMMANDS = frozenset(
    {"add-locations", "batch-edit", "import", "push-exif", "sync", "timewarp"}
)
"""Commands that change the Photos library; only available with `--allow-write`."""

LIBRARY_COMMANDS = frozenset(
    {
        "albums",
        "dump",
        "export",
        "info",
        "keywords",
        "labels",
        "orphans",
        "persons",
        "places",
        "push-exif",
        "query",
        "show",
    }
)
"""Commands that take `--library`, so the server's configured library can be passed on."""

HIDDEN_COMMANDS = frozenset({"add-locations", "export", "push-exif", "query", "sync"})
"""Commands that take `--not-hidden`, so hidden photos can be left out of their results."""

NO_PHOTO_DATA_COMMANDS = frozenset(
    {"about", "dbversion", "list", "orphans", "show", "uuid", "version"}
)
"""Commands whose output says nothing about photos the user did not select themselves.

Every other command must be able to filter hidden photos (`HIDDEN_COMMANDS`), or it is refused
unless the server includes hidden photos. That covers commands that list photos from earlier
exports (`exportdb`, `exiftool`) and write commands that can print the photos they select
(`timewarp --inspect`, `batch-edit`, `import`).
"""

BLOCKED_OPTIONS = frozenset(
    {
        # run a shell command, a Python file, or a Mako template (which embeds Python)
        "--cleanup-command",
        "--function",
        "--post-command",
        "--post-function",
        "--query-eval",
        "--query-function",
        "--sidecar-template",
        # run an arbitrary executable, or pass it arbitrary options
        "--exiftool-option",
        "--exiftool-path",
        # load options from a file, which could contain any of the above
        "--db-config",
        "--load-config",
        # debugger hooks
        "--breakpoint",
        "--debug",
        "--profile",
        "--profile-sort",
        "--watch",
    }
)
"""Options that execute code. Never allowed, even with `--allow-write`."""

BLOCKED_SHORT_OPTIONS = {"import": "p", "timewarp": "Fe"}
"""Single-letter aliases of blocked options, per command."""

WRITE_OPTIONS = frozenset(
    {
        "--add-exported-to-album",
        "--add-missing-to-album",
        "--add-skipped-to-album",
        "--add-to-album",
        "--cleanup",
        "--delete-file",
        "--delete-uuid",
        # replace files in the export folder, or write to a file the caller names
        "--output",
        "--overwrite",
        "--report",
        "--save-config",
    }
)
"""Options on read commands that change the library, or overwrite or delete files."""

WRITE_SHORT_OPTIONS = {"compare": "o"}
"""Single-letter aliases of write options, per command."""

MAX_OUTPUT = 50_000
MAX_HELP = 30_000
TIMEOUT_SECONDS = 600

_NEEDS_WRITE = "start the server with --allow-write to enable it"
_CHANGES_DATA = "changes the library, or overwrites or deletes files"
_NEEDS_HIDDEN = "start the server with --include-hidden to see hidden photos"
_CRASH_LOG = "osxphotos_crash.log"


def check(
    command: str, args: list[str], *, allow_write: bool, include_hidden: bool = False
) -> None:
    """Raise `ValueError` if this command line must not be run."""
    _check_command(command)
    if command in WRITE_COMMANDS and not allow_write:
        raise ValueError(f"'{command}' changes the Photos library; {_NEEDS_WRITE}.")
    if not include_hidden and command not in HIDDEN_COMMANDS | NO_PHOTO_DATA_COMMANDS:
        raise ValueError(f"'{command}' cannot leave out hidden photos; {_NEEDS_HIDDEN}.")
    for arg in args:
        if "function:" in arg:
            raise ValueError("Template functions ('function:') are not allowed: they run code.")
        if arg.startswith("--"):
            option = arg.split("=", 1)[0]
            if option in BLOCKED_OPTIONS:
                raise ValueError(f"Option {option} is not allowed: it can run code.")
            if option in WRITE_OPTIONS and not allow_write:
                raise ValueError(f"Option {option} {_CHANGES_DATA}; {_NEEDS_WRITE}.")
            if option == "--hidden" and not include_hidden:
                raise ValueError(f"Hidden photos are left out; {_NEEDS_HIDDEN}.")
        elif arg.startswith("-") and arg[1:2].isalpha():
            blocked = set(arg[1:]) & set(BLOCKED_SHORT_OPTIONS.get(command, ""))
            if blocked:
                raise ValueError(f"Option -{min(blocked)} is not allowed: it can run code.")
            writes = set(arg[1:]) & set(WRITE_SHORT_OPTIONS.get(command, ""))
            if writes and not allow_write:
                raise ValueError(f"Option -{min(writes)} {_CHANGES_DATA}; {_NEEDS_WRITE}.")


def build_args(
    command: str, args: list[str], *, library: str | None, include_hidden: bool = False
) -> list[str]:
    """Prepend the command and, where they apply, the configured library and hidden filter."""
    built = [command]
    explicit_library = any(arg.split("=", 1)[0] in ("--library", "--db") for arg in args)
    if library and command in LIBRARY_COMMANDS and not explicit_library:
        built += ["--library", library]
    if command in HIDDEN_COMMANDS and not include_hidden:
        # Always added, and before the caller's arguments: a "--not-hidden" among them may be
        # the value of another option, or follow "--", and would then filter nothing.
        built.append("--not-hidden")
    return built + args


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[output truncated: {len(text) - limit} more characters]"


def run(
    command: str,
    args: list[str],
    *,
    allow_write: bool,
    library: str | None = None,
    include_hidden: bool = False,
) -> str:
    """Run one osxphotos command and return its output."""
    check(command, args, allow_write=allow_write, include_hidden=include_hidden)
    built = build_args(command, args, library=library, include_hidden=include_hidden)
    return truncate(_osxphotos(built))


def command_help(
    command: str | None,
    *,
    allow_write: bool,
    topic: str | None = None,
    include_hidden: bool = False,
) -> str:
    """Help for one command (optionally only the options about `topic`), or the command list."""
    if not command:
        usable = HIDDEN_COMMANDS | NO_PHOTO_DATA_COMMANDS
        if include_hidden:
            usable = READ_COMMANDS | WRITE_COMMANDS
        unusable = (READ_COMMANDS | WRITE_COMMANDS) - usable
        state = "enabled" if allow_write else f"disabled; {_NEEDS_WRITE}"
        lines = [
            f"Commands: {_names(READ_COMMANDS & usable)}",
            f"Commands that change the library ({state}): {_names(WRITE_COMMANDS & usable)}",
        ]
        if unusable:
            lines.append(
                f"Commands that cannot leave out hidden photos (disabled; {_NEEDS_HIDDEN}): "
                f"{_names(unusable)}"
            )
        lines.append("Call osxphotos_help with a command name to see its options.")
        return "\n".join(lines)
    _check_command(command)
    if topic:
        return truncate(_osxphotos(["help", command, topic]))
    text = _osxphotos([command, "--help"])
    return text if len(text) <= MAX_HELP else option_index(text)


def option_index(help_text: str) -> str:
    """Shrink a very long help text to its introduction and the names of its options."""
    intro, _, rest = help_text.partition("\nOptions:\n")
    names = []
    for line in rest.splitlines():
        if line and not line.startswith(" "):
            break  # end of the options; reference material follows
        if line.startswith("  -"):
            names.append(re.split(r"\s{2,}", line.strip())[0])
    listing = "\n".join(names)
    return (
        f"{intro.strip()}\n\n"
        "Options (names only, because the full help is very long; call osxphotos_help again "
        "with a topic such as 'sidecar' to read the descriptions of matching options):\n"
        f"{listing}\n"
    )


def _names(commands: frozenset[str]) -> str:
    return ", ".join(sorted(commands))


def _check_command(command: str) -> None:
    if command not in READ_COMMANDS | WRITE_COMMANDS:
        raise ValueError(
            f"'{command}' is not available through this server. "
            f"Available: {', '.join(sorted(READ_COMMANDS | WRITE_COMMANDS))}."
        )


def _osxphotos(args: list[str]) -> str:
    crash_log = Path(_CRASH_LOG)
    had_crash_log = crash_log.exists()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "osxphotos", *args],
            capture_output=True,
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"osxphotos {args[0]} timed out after {TIMEOUT_SECONDS}s.") from None
    finally:
        # osxphotos writes its crash log to the working directory, which is the user's project
        if not had_crash_log:
            crash_log.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = _tidy(result.stderr) or _tidy(result.stdout)
        raise RuntimeError(truncate(detail) or f"osxphotos exited with code {result.returncode}.")
    return _tidy(result.stdout) or _tidy(result.stderr)


def _tidy(text: str) -> str:
    """Drop the padding osxphotos adds to the end of lines."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()
