from __future__ import annotations

import sys

import anyio
from k.io_helpers.shell import ShellSession

async def main() -> None:
    async with ShellSession("bash", timeout_seconds=1) as session:
        user_input = ""
        while True:
            user_input += "\n"

            stdout, stderr, code = await session.next(user_input.encode())
            print(f"STDOUT:\n{stdout.decode()}")
            print(f"STDERR:\n{stderr.decode()}", file=sys.stderr)
            print(f"Exit Code: {code}")
            if code is not None:
                break

            user_input = input("bash> ")


if __name__ == "__main__":
    anyio.run(main)

