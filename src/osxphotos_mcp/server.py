"""The MCP server: five typed tools for everyday use, two that reach the rest of osxphotos."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import passthrough
from .photos import Library

INSTRUCTIONS = (
    "Tools for the user's Apple Photos library, built on osxphotos. "
    "Use search_photos, get_photo, list_albums, list_people_and_keywords and export_photos first. "
    "For anything they cannot do, read osxphotos_help and then call run_osxphotos."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@contextmanager
def _explained() -> Iterator[None]:
    """Pass the reason for a failure on to the model, which the SDK otherwise withholds."""
    try:
        yield
    except (ValueError, RuntimeError, OSError) as error:
        raise ToolError(str(error)) from error


def build_server(
    library: str | None = None, allow_write: bool = False, include_hidden: bool = False
) -> MCPServer:
    """Create the server for one Photos library (the system library when `library` is None)."""
    photos = Library(library, include_hidden=include_hidden)
    server = MCPServer(
        "osxphotos",
        title="Apple Photos (osxphotos)",
        version=version("osxphotos-mcp"),
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def search_photos(
        text: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        album: str | None = None,
        person: str | None = None,
        keyword: str | None = None,
        place: str | None = None,
        favorite: bool | None = None,
        media_type: Literal["photo", "video"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Find photos and videos, newest first. All filters are optional and combine with AND.

        text: every word must occur in the title, description, filename, people, keywords,
        albums, labels or place. Dates are ISO (YYYY-MM-DD, optionally with a time) in the
        photo's local time; to_date includes that whole day. album, person and keyword are
        exact names, ignoring case. Returns the total number of matches and up to `limit`
        (max 100) of them, starting at `offset`.
        """
        with _explained():
            return photos.search(
                text=text,
                from_date=from_date,
                to_date=to_date,
                album=album,
                person=person,
                keyword=keyword,
                place=place,
                favorite=favorite,
                media_type=media_type,
                limit=limit,
                offset=offset,
            )

    @server.tool(annotations=READ_ONLY)
    def get_photo(uuid: str) -> dict[str, Any]:
        """Full details of one photo: metadata, albums, labels, camera, location and file paths."""
        with _explained():
            return photos.get_photo(uuid)

    @server.tool(annotations=READ_ONLY)
    def list_albums() -> dict[str, Any]:
        """All albums, including shared albums, with their folder and number of photos."""
        with _explained():
            return {"albums": photos.list_albums()}

    @server.tool(annotations=READ_ONLY)
    def list_people_and_keywords() -> dict[str, Any]:
        """Names of people and keywords in the library with photo counts, most used first."""
        with _explained():
            return photos.list_people_and_keywords()

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    def export_photos(uuids: list[str], destination: str) -> dict[str, Any]:
        """Copy photos to a folder, using the edited version where one exists.

        destination is an absolute folder path. Never overwrites existing files. Photos that
        are not downloaded from iCloud or cannot be written are reported as skipped. To fetch
        those from iCloud, or for more control, use run_osxphotos: command "export" with args
        [destination, "--uuid", uuid, "--download-missing"].
        """
        with _explained():
            return photos.export(uuids, destination)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def osxphotos_help(command: str | None = None, topic: str | None = None) -> str:
        """Help for one osxphotos command, or the list of available commands if none is given.

        Pass a topic (one word, for example "location") to see only the options about it.
        """
        with _explained():
            return passthrough.command_help(command, allow_write=allow_write, topic=topic)

    @server.tool(
        annotations=ToolAnnotations(destructive_hint=allow_write, open_world_hint=False),
        structured_output=False,
    )
    def run_osxphotos(command: str, args: list[str] | None = None) -> str:
        """Run an osxphotos command line command and return its output.

        Pass options as separate items, for example command "query" with args
        ["--no-location", "--json"]. Call osxphotos_help first to see a command's options.
        """
        try:
            with _explained():
                return passthrough.run(
                    command,
                    args or [],
                    allow_write=allow_write,
                    library=library,
                    include_hidden=include_hidden,
                )
        finally:
            if command in passthrough.WRITE_COMMANDS:
                photos.refresh()

    return server
