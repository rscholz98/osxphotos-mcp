import sys

import anyio
from mcp import Client, StdioServerParameters

from osxphotos_mcp.server import build_server

WEDDING = "E9BC5C36-7CD1-40A1-A72B-8B8FAC227D51"


def call(server, tool: str, arguments: dict | None = None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(run)


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


def test_server_offers_exactly_the_seven_tools():
    async def run():
        async with Client(build_server()) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(run)}
    assert set(tools) == {
        "search_photos",
        "get_photo",
        "list_albums",
        "list_people_and_keywords",
        "export_photos",
        "osxphotos_help",
        "run_osxphotos",
    }
    assert all(tool.description for tool in tools.values())
    assert tools["search_photos"].annotations.read_only_hint is True
    assert tools["run_osxphotos"].annotations.read_only_hint is not True


def test_search_photos_returns_matches_as_structured_data(library_path):
    result = call(build_server(library_path), "search_photos", {"person": "Maria", "limit": 5})
    assert not result.is_error
    assert result.structured_content["total"] == 1
    assert result.structured_content["photos"][0]["uuid"] == WEDDING


def test_search_photos_needs_no_arguments(library_path):
    result = call(build_server(library_path), "search_photos")
    assert result.structured_content["total"] == 14
    assert len(result.structured_content["photos"]) == 14


def test_get_photo_with_unknown_uuid_is_a_tool_error(library_path):
    result = call(build_server(library_path), "get_photo", {"uuid": "nope"})
    assert "No photo with UUID" in error_text(result)


def test_list_albums_and_people_tools_return_data(library_path):
    server = build_server(library_path)
    albums = call(server, "list_albums").structured_content["albums"]
    assert {"title": "Pumpkin Farm", "count": 3} in albums
    people = call(server, "list_people_and_keywords").structured_content["people"]
    assert people["Katie"] == 3


def test_export_photos_writes_to_the_destination(library_path, tmp_path):
    result = call(
        build_server(library_path),
        "export_photos",
        {"uuids": [WEDDING, "nope"], "destination": str(tmp_path)},
    )
    assert len(result.structured_content["exported"]) == 1
    assert len(list(tmp_path.iterdir())) == 1
    assert result.structured_content["skipped"] == [{"uuid": "nope", "reason": "not found"}]


def test_run_osxphotos_uses_the_configured_library(library_path):
    result = call(build_server(library_path), "run_osxphotos", {"command": "albums"})
    assert "Pumpkin Farm" in result.content[0].text


def test_run_osxphotos_refuses_options_that_run_code():
    result = call(
        build_server(), "run_osxphotos", {"command": "query", "args": ["--query-eval", "True"]}
    )
    assert "not allowed" in error_text(result)


def test_library_changes_are_refused_by_default():
    result = call(build_server(), "run_osxphotos", {"command": "batch-edit"})
    assert "--allow-write" in error_text(result)


def test_library_changes_pass_the_guard_with_allow_write():
    # `--help` keeps this from touching anything: it only proves the command is let through.
    result = call(
        build_server(allow_write=True),
        "run_osxphotos",
        {"command": "batch-edit", "args": ["--help"]},
    )
    assert not result.is_error
    assert "Usage" in result.content[0].text


def test_osxphotos_help_lists_commands_and_explains_one():
    server = build_server()
    assert "query" in call(server, "osxphotos_help").content[0].text
    assert "--from-date" in call(server, "osxphotos_help", {"command": "query"}).content[0].text


def test_installed_command_serves_over_stdio(library_path):
    command = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from osxphotos_mcp import main; main()", "--library", library_path],
    )
    result = call(command, "search_photos", {"favorite": True})
    assert result.structured_content["photos"][0]["filename"] == "wedding.jpg"


def test_osxphotos_help_narrows_a_command_to_a_topic():
    result = call(build_server(), "osxphotos_help", {"command": "query", "topic": "location"})
    assert "--no-location" in result.content[0].text
    assert "--favorite" not in result.content[0].text
