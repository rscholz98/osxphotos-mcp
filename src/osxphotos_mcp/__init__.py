"""A slim MCP server for Apple Photos, built on osxphotos."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="osxphotos-mcp", description=__doc__)
    parser.add_argument("--library", help="path to a Photos library (default: the system library)")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="enable osxphotos commands and options that change the Photos library or an "
        "export database, or overwrite or delete files",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="include photos that are hidden in Photos",
    )
    parser.add_argument(
        "--preload",
        action="store_true",
        help="load the library when the server starts, not on first use",
    )
    options = parser.parse_args()

    from .server import build_server

    server = build_server(
        options.library, options.allow_write, options.include_hidden, options.preload
    )
    server.run()
