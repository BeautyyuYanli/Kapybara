"""Run local async exec; optionally read a task from an already running pueued.

python examples/async_local.py [--pueue-config /path/to/pueue.yml]
"""

import argparse
import asyncio

from opensol import aio


async def main(config_file: str | None) -> None:
    connection = aio.local()
    result = await connection.exec(["printf", "%s", "OpenSOL\n"], timeout=5)
    print(result.stdout)
    if config_file is None:
        return
    manager = aio.pueue(connection, config_file=config_file)
    handle = await manager.spawn(["python3", "-c", "print('managed task')"])
    try:
        async for event in handle:
            if event.outcome is None:
                print(event.channel, event.data)
            else:
                print("completed", event.outcome)
    finally:
        await handle.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pueue-config")
    asyncio.run(main(parser.parse_args().pueue_config))
