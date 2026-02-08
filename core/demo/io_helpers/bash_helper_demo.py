from __future__ import annotations

import sys

import anyio
from k.io_helpers.bash_helper import BashSession

async def main() -> None:
    async with BashSession("bash", timeout_seconds=3) as session:
        while True:
            user_input = input("bash> ")
            user_input += "\n"

            stdout, stderr, code = await session.next(user_input.encode())
            print(f"STDOUT:\n{stdout.decode()}")
            print(f"STDERR:\n{stderr.decode()}", file=sys.stderr)
            print(f"Exit Code: {code}")
            if code is not None:
                break


if __name__ == "__main__":
    anyio.run(main)

