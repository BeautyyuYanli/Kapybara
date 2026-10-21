"""Create a Docker sandbox and observe native state with overall deadlines.

python examples/async_sandbox.py IMAGE
Requires a caller-owned Docker Engine and an existing image with sleep and true.
"""

import argparse
import asyncio

import opensol as sdk
from opensol import aio


async def main(image: str) -> None:
    backend = await aio.Backend.open(sdk.DockerConfig())
    manager = await aio.SandboxManager.open(backend)
    sandbox = await manager.create(sdk.DockerSandboxSpec(image, command=["sleep", "infinity"], labels={"project": "opensol-example"}))
    try:
        # Discovery fields come from the native list, without per-item inspect or SDK metadata.
        for info in await manager.list():
            if info.labels.get("project") == "opensol-example":
                rebound = manager.get(info.sandbox)
                print(info.name, rebound.ref.id, info.state)
        async with asyncio.timeout(30):
            await sandbox.start()
            while True:
                info = await sandbox.inspect()
                if info.state == "running":
                    break
                if info.state in {"failed", "completed"}:
                    raise RuntimeError(f"sandbox {info.state}: {info.native_state}")
                await asyncio.sleep(0.2)
        # Running is a native observation; a later exec can still race or fail to connect.
        connection = await aio.Connection.open(backend, target=sandbox.ref)
        result = await connection.exec(["true"], timeout=5)
        print("exec succeeded:", result.success)
    finally:
        async with asyncio.timeout(30):
            await sandbox.delete()
            while True:
                try:
                    await sandbox.inspect()
                except sdk.NotFoundError:
                    break
                await asyncio.sleep(0.2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    asyncio.run(main(parser.parse_args().image))
