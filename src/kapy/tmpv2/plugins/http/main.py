"""Standalone HTTP entry; the dispatcher creates neither a loop nor a child process."""

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from .app import create_app
from .settings import HttpSettings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kapy plugin http")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--frontend-dist", type=Path)
    args = parser.parse_args(argv)
    try:
        values = {
            key: value
            for key in ("host", "port", "frontend_dist")
            if (value := getattr(args, key)) is not None
        }
        settings = HttpSettings(**values)
    except ValidationError:
        parser.error("Invalid HTTP configuration; check KAPY_HTTP_* and KAPY_CONTROL_TOKEN")
    logging.basicConfig(level=settings.common.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=int(settings.shutdown_timeout),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
