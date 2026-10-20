"""Run local async exec; optionally read a task from an already running pueued.

python examples/async_local.py [--pueue-config /path/to/pueue.yml --pueue-directory /path/to/data]
"""

import argparse
import asyncio

from opensol import aio


async def main(config_file: str | None, pueue_directory: str | None) -> None:
    connection = aio.local()
    result = await connection.exec(["printf", "%s", "OpenSOL\n"], timeout=5)
    print(result.stdout)
    if config_file is None:
        return
    manager = aio.pueue(connection, config_file=config_file, pueue_directory=pueue_directory)
    process, subscription = await manager.spawn(["python3", "-c", "print('managed task')"])
    print("native task ID", process.ref.id)
    try:
        async for event in subscription:
            if event.outcome is None:
                print(event.channel, event.data)
            else:
                print("completed", event.outcome)
    finally:
        await subscription.aclose()
    if process.output.capabilities()["tail"]:
        last = await process.output.tail(max_bytes=4096)
        previous = await process.output.read(cursor=last.before, direction="backward", max_bytes=4096)
        print("preceding page", previous.chunks, "last page", last.chunks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pueue-config")
    parser.add_argument("--pueue-directory")
    args = parser.parse_args()
    asyncio.run(main(args.pueue_config, args.pueue_directory))
