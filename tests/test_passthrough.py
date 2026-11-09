import json
import subprocess
import sys

import pytest

from osxphotos_mcp import passthrough


def test_read_command_is_allowed():
    passthrough.check("query", ["--json", "--favorite"], allow_write=False)


@pytest.mark.parametrize("command", ["repl", "run", "install", "inspect", "no-such-command"])
def test_interactive_and_unknown_commands_are_rejected(command):
    with pytest.raises(ValueError, match="not available"):
        passthrough.check(command, [], allow_write=True)


def test_write_command_is_rejected_without_allow_write():
    with pytest.raises(ValueError, match="--allow-write"):
        passthrough.check("batch-edit", ["--title", "x"], allow_write=False)


def test_write_command_is_allowed_with_allow_write():
    passthrough.check("batch-edit", ["--title", "x"], allow_write=True, include_hidden=True)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("export", ["/tmp/x", "--post-command", "exported", "rm -rf {filepath}"]),
        ("export", ["/tmp/x", "--post-function", "evil.py::fn"]),
        ("export", ["/tmp/x", "--load-config", "evil.toml"]),
        ("export", ["/tmp/x", "--sidecar-template", "evil.mako", "out.txt"]),
        ("export", ["/tmp/x", "--exiftool-path=/tmp/evil"]),
        ("export", ["/tmp/x", "--directory", "{function:evil.py::fn}"]),
        ("query", ["--query-eval=__import__('os').system('id')"]),
        ("query", ["--query-function", "evil.py::fn"]),
        ("query", ["--breakpoint", "osxphotos.cli"]),
    ],
)
def test_options_that_execute_code_are_rejected_even_with_allow_write(command, args):
    with pytest.raises(ValueError, match="not allowed"):
        passthrough.check(command, args, allow_write=True, include_hidden=True)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("import", ["-p", "/tmp/evil", "photo.jpg"]),
        ("import", ["-np", "/tmp/evil", "photo.jpg"]),
        ("timewarp", ["-F", "evil.py::fn"]),
    ],
)
def test_short_aliases_of_code_executing_options_are_rejected(command, args):
    with pytest.raises(ValueError, match="not allowed"):
        passthrough.check(command, args, allow_write=True, include_hidden=True)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("query", ["--add-to-album", "Best"]),
        ("export", ["/tmp/x", "--cleanup"]),
        ("exportdb", ["/tmp/x", "--delete-uuid=ABC"]),
    ],
)
def test_options_that_change_data_need_allow_write(command, args):
    with pytest.raises(ValueError, match="--allow-write"):
        passthrough.check(command, args, allow_write=False, include_hidden=True)
    passthrough.check(command, args, allow_write=True, include_hidden=True)


def test_configured_library_is_passed_to_commands_that_accept_it():
    args = passthrough.build_args("query", ["--json"], library="/L", include_hidden=True)
    assert args == ["query", "--library", "/L", "--json"]


@pytest.mark.parametrize("flag", ["--library", "--db", "--library=/other"])
def test_explicit_library_argument_wins_over_configured_library(flag):
    args = [flag, "/other"] if "=" not in flag else [flag]
    built = passthrough.build_args("query", args, library="/L", include_hidden=True)
    assert built == ["query", *args]


def test_library_is_not_passed_to_commands_without_library_option():
    assert passthrough.build_args("list", ["--json"], library="/L") == ["list", "--json"]


def test_long_output_is_truncated_with_a_note():
    out = passthrough.truncate("x" * 60_000)
    assert len(out) < 51_000
    assert "10000 more characters" in out


def test_short_output_is_returned_unchanged():
    assert passthrough.truncate("hello") == "hello"


def test_run_returns_the_command_output(library_path):
    out = passthrough.run(
        "albums", ["--json"], allow_write=False, library=library_path, include_hidden=True
    )
    assert "Pumpkin Farm" in json.loads(out)["albums"]


def test_run_raises_with_the_error_text_when_the_command_fails():
    with pytest.raises(RuntimeError, match="(?i)no such option"):
        passthrough.run("query", ["--definitely-not-an-option"], allow_write=False)


def test_run_refuses_a_blocked_option_before_running_anything():
    with pytest.raises(ValueError, match="not allowed"):
        passthrough.run("query", ["--query-eval", "True"], allow_write=False)


def test_help_for_a_command_lists_its_options():
    assert "--from-date" in passthrough.command_help("query", allow_write=False)


def test_help_without_a_command_lists_available_commands():
    text = passthrough.command_help(None, allow_write=False)
    assert "query" in text
    assert "repl" not in text
    assert "batch-edit" in text and "--allow-write" in text


def test_help_for_an_unavailable_command_is_rejected():
    with pytest.raises(ValueError, match="not available"):
        passthrough.command_help("repl", allow_write=True)


@pytest.mark.parametrize("command", sorted(passthrough.LIBRARY_COMMANDS))
def test_commands_that_get_the_library_really_accept_it(command):
    text = subprocess.run(
        [sys.executable, "-m", "osxphotos", command, "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert any(line.strip().startswith("--library") for line in text.splitlines())


SAMPLE_HELP = """\
Usage: osxphotos export [OPTIONS] DEST

  Export photos from the Photos database.

Options:
  --library, --db PHOTOS_LIBRARY_PATH
                                  Specify path to Photos library.
  -V, --verbose                   Print verbose output.
  --directory DIRECTORY           Optional template for the export
                                  directory.

Templating System

    The templating system converts one or more template statements.
"""


def test_option_index_keeps_usage_and_option_names_but_not_descriptions():
    index = passthrough.option_index(SAMPLE_HELP)
    assert "Usage: osxphotos export [OPTIONS] DEST" in index
    assert "Export photos from the Photos database." in index
    assert "--library, --db PHOTOS_LIBRARY_PATH\n" in index
    assert "-V, --verbose\n" in index
    assert "--directory DIRECTORY\n" in index
    assert "Print verbose output" not in index
    assert "Templating System" not in index
    assert "topic" in index


def test_help_too_long_for_one_answer_becomes_an_option_index():
    text = passthrough.command_help("export", allow_write=False)
    assert len(text) < 20_000
    assert "--skip-edited\n" in text
    assert "topic" in text


def test_help_with_a_topic_returns_only_matching_options():
    text = passthrough.command_help("export", topic="sidecar", allow_write=False)
    assert "Options that match 'sidecar'" in text
    assert "--sidecar-drop-ext" in text
    assert len(text) < 20_000


def test_hidden_photos_are_filtered_out_of_commands_that_list_photos():
    assert passthrough.build_args("query", ["--json"], library=None) == [
        "query",
        "--not-hidden",
        "--json",
    ]


def test_hidden_filter_is_added_even_when_the_arguments_already_contain_it():
    # "--not-hidden" here is the value of --title, so it must not count as the filter.
    assert passthrough.build_args("query", ["--title", "--not-hidden"], library=None) == [
        "query",
        "--not-hidden",
        "--title",
        "--not-hidden",
    ]


def test_hidden_filter_is_not_added_when_hidden_photos_are_included():
    assert passthrough.build_args("query", [], library=None, include_hidden=True) == ["query"]


def test_only_these_read_commands_cannot_leave_out_hidden_photos():
    unfiltered = passthrough.READ_COMMANDS - passthrough.HIDDEN_COMMANDS
    assert unfiltered - passthrough.NO_PHOTO_DATA_COMMANDS == {
        "albums",
        "compare",
        "dump",
        "exiftool",
        "exportdb",
        "info",
        "keywords",
        "labels",
        "persons",
        "places",
    }


UNFILTERED = ["albums", "compare", "dump", "exiftool", "exportdb", "info", "keywords", "labels"]
UNFILTERED += ["persons", "places", "batch-edit", "import", "timewarp"]


@pytest.mark.parametrize("command", UNFILTERED)
def test_commands_that_cannot_leave_out_hidden_photos_are_refused(command):
    # Also with writes enabled: `timewarp --inspect --year 2025` would list hidden photos.
    with pytest.raises(ValueError, match="--include-hidden"):
        passthrough.check(command, [], allow_write=True)
    passthrough.check(command, [], allow_write=True, include_hidden=True)


@pytest.mark.parametrize("command", ["about", "list", "show", "uuid", "query", "export", "sync"])
def test_commands_that_filter_or_show_no_photo_data_stay_available(command):
    passthrough.check(command, [], allow_write=True)


def test_hidden_photos_cannot_be_requested_unless_the_server_includes_them():
    with pytest.raises(ValueError, match="--include-hidden"):
        passthrough.check("query", ["--hidden"], allow_write=False)
    passthrough.check("query", ["--hidden"], allow_write=False, include_hidden=True)


def test_run_leaves_out_hidden_photos_by_default(library_path):
    count = ["--count"]
    assert passthrough.run("query", count, allow_write=False, library=library_path) == "13"
    with_hidden = passthrough.run(
        "query", count, allow_write=False, library=library_path, include_hidden=True
    )
    assert with_hidden == "14"


@pytest.mark.parametrize("command", sorted(passthrough.HIDDEN_COMMANDS))
def test_commands_that_get_the_hidden_filter_really_accept_it(command):
    text = subprocess.run(
        [sys.executable, "-m", "osxphotos", command, "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert any(line.strip().startswith("--not-hidden") for line in text.splitlines())


def test_output_has_no_trailing_padding():
    text = passthrough.command_help("timewarp", allow_write=False)
    assert all(line == line.rstrip() for line in text.splitlines())


def test_crash_log_of_a_failed_command_is_not_left_in_the_working_directory(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.chdir(tmp_path)
    sqlite3.connect(tmp_path / "not-an-export.db").execute("create table t (x)").connection.close()
    with pytest.raises(RuntimeError):
        passthrough.run(
            "exportdb",
            ["not-an-export.db", "--sql", "select 1"],
            allow_write=False,
            include_hidden=True,
        )
    assert not (tmp_path / "osxphotos_crash.log").exists()


def test_command_list_separates_commands_that_need_hidden_photos():
    lines = passthrough.command_help(None, allow_write=True).splitlines()
    assert lines[0].startswith("Commands: ") and "labels" not in lines[0] and "query" in lines[0]
    assert "sync" in lines[1] and "timewarp" not in lines[1]
    assert "--include-hidden" in lines[2] and "labels" in lines[2] and "timewarp" in lines[2]


def test_command_list_has_no_hidden_section_when_hidden_photos_are_included():
    text = passthrough.command_help(None, allow_write=True, include_hidden=True)
    assert "--include-hidden" not in text
    assert "labels" in text.splitlines()[0]
